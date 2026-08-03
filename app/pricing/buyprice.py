"""Acquisition layer — cascade comp search + buy price.

This sits ON TOP of the v4 valuation (it uses the v4 model value as an input;
it is NOT the valuation model itself). Client spec, confirmed 29–30 Jun 2026.

Buy price:
  1. Cascade comp search (tightest tier with 2+ comps), tiers 1→6:
       1  suburb + type + beds±1 + baths±1 + land±15%
       2  suburb + type + beds±1 + land±15%
       3  suburb + type + beds±1
       4  suburb + type
       5  district + type + beds±1
       6  district + type
     Comps exclude non-arm's-length sales (freehold houses sold < 75% of CV).
  2. area_value = v4_value × average(each comp's sale ÷ its own v4 value)
       (ratios outside [0.5, 2.0] dropped — that comp's CV is unreliable)
  3. If no comps match at any tier → area_value = v4_value (already local-data based)
  4. buy_price = 0.95 × MIN(asking, area_value)   (never above asking)
                 = 0.95 × area_value              (when no asking price)
  When there is no model value, fall back to CV × the area's sale/CV ratio
  (see cv_ratio_for), then to the asking price alone.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .glm import canonical_type, predict, title_code

DISCOUNT = 0.95
RATIO_LO, RATIO_HI = 0.5, 2.0   # drop comps whose sale/v4 ratio is implausible
RATIO_CV_LO, RATIO_CV_HI = 0.3, 3.0  # drop comps with a broken council CV (sale/CV)
LAND_TOL = 0.15                 # ±15%
MIN_COMPS = 2
# "Only use sales on/after 07/01/2025" — the v4 tool's outer recency bound. Sales
# older than this never enter the engine at all.
SOLD_FROM_YEAR = 2025
SOLD_FROM_MONTH = 7

# Expanding comp window for the sale/CV ratio. Take the most recent window that
# holds at least RATIO_MIN_COMPS sales, and only widen when it does not.
#
# Prices drift 1-2% over a few months, so a stale comp carries a small bias. But
# a median over 3 sales carries a much larger variance — near-identical houses
# sell 7.2% apart at the median. Measured over 5 holdout splits of 2026 sales:
#     2->3->6->12 months, need 3    8.13%   <- freshest, and the worst
#     all sales since Jul-2025      8.11%   <- previous behaviour
#     3->6->12 months, need 3       7.99%
#     6->12 months, need 3          7.91%
#     6->12->24 months, need 8      7.70%   <- chosen
# In a faster-moving market the right response is to raise the comp requirement,
# not to shorten the window.
RATIO_WINDOWS_MONTHS = (6, 12, 24)
RATIO_MIN_COMPS = 8

# Direct sold-price match, used when the CV cannot be trusted: same suburb, type,
# beds and baths, with land and floor within this tolerance.
MATCH_TOL = 0.20

# Like-for-like comp spec: same suburb, type, beds and baths, with land and floor
# within this tolerance. Their sale/CV ratios drive the value.
SPEC_TOL = 0.25
SPEC_MIN_COMPS = 3
SPEC_RATIO_LO, SPEC_RATIO_HI = 0.75, 1.25   # v4 tool: exclude sold/CV beyond these
MATCH_MIN_COMPS = 2
# Shrinkage weights for the sale/CV ratio. A suburb's own ratio is trusted in
# proportion to its sale count: with K_SUBURB sales it carries half the weight,
# the rest coming from the district (itself shrunk toward the global median).
# Tuned by holdout backtest over 5 random splits — see
# tests/test_valuation_accuracy.py, which fails if this stops beating raw CV.
K_SUBURB = 30
K_DISTRICT = 20
# If the modelled value is more than this far below the asking price, distrust it
# and re-anchor on CV × the area's sale/CV ratio for the same property type.
MODEL_VS_ASKING_MAX_GAP = 0.30

# Title classes — leasehold / cross-lease / unit-title sell well below their CV,
# so the sale/CV ratio must be measured within the same class, not blended.
_TCLASS = {1: "FH", 2: "LH", 3: "CL", 4: "UT"}


def _title_bucket(title) -> str:
    # Sold data stores the numeric code as a string ("1.0"/"3.0"); for-sale data
    # stores the text ("Freehold"/"Cross-Lease"). Handle both.
    s = str(title).strip().lower()
    if s in ("1", "1.0"):
        return "FH"
    if s in ("2", "2.0"):
        return "LH"
    if s in ("3", "3.0"):
        return "CL"
    if s in ("4", "4.0"):
        return "UT"
    return _TCLASS.get(title_code(title), "OT")


@dataclass
class BuyResult:
    buy_price: float | None
    area_value: float | None
    comp_tier: int | None        # 1–6, or None when it fell to the v4 fallback
    comps_matched: int


def _age_from(row) -> float | None:
    yb = row.get("building_age")
    try:
        yb = float(yb)
    except (TypeError, ValueError):
        return None
    if 1800 <= yb <= 2030:
        return 2026 - int(yb)
    if 0 < yb < 200:
        return yb
    return None


class CompEngine:
    """Holds the sold dataset, pre-computes each comp's v4 value, and answers
    buy-price queries via the cascade. Build once per ingest, reuse per row."""

    def __init__(self, sold_df: pd.DataFrame):
        df = sold_df.copy()
        # Normalise the columns we rely on (sold CSVs use the scraper names).
        rename = {
            "key_bedrooms": "beds", "key_bathrooms": "baths", "key_carspaces": "cars",
            "key_floor_area": "floor_area_m2", "key_land_area": "land_area_m2",
            "price_numeric": "sale_price",
        }
        for src, dst in rename.items():
            if src in df.columns and dst not in df.columns:
                df[dst] = df[src]
        for c in ("beds", "baths", "cars", "floor_area_m2", "land_area_m2",
                  "sale_price", "cv_numeric", "building_age"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df[(df.get("sale_price") > 0)].copy()

        # Recency — "only use sales on/after 07/01/2025" (v4 tool constant), i.e.
        # the last six months of 2025 plus all of 2026. The old filter was a
        # string match on "2025"/"2026", which swept in January 2025 sales when
        # the market was materially different (2025 ran at 0.973 sale/CV against
        # 2026's 0.986). Dates arrive as M/D/YYYY text.
        date_col = next((c for c in ("sold_date", "sold_listing_date") if c in df.columns), None)
        if date_col is not None:
            parts = df[date_col].astype(str).str.split("/", expand=True)
            if parts.shape[1] >= 3:
                mm = pd.to_numeric(parts[0], errors="coerce")
                yy = pd.to_numeric(parts[2], errors="coerce")
                recent = ((yy > SOLD_FROM_YEAR)
                          | ((yy == SOLD_FROM_YEAR) & (mm >= SOLD_FROM_MONTH)))
                if recent.any():
                    df = df[recent.fillna(False)].copy()

        df["ct"] = df["property_type"].map(canonical_type)

        # Month index (year*12 + month) so the ratio lookup can take a window.
        if date_col is not None and parts.shape[1] >= 3:
            df["_t"] = (pd.to_numeric(parts[2], errors="coerce") * 12
                        + pd.to_numeric(parts[0], errors="coerce")).reindex(df.index)
        else:
            df["_t"] = float("nan")

        # Arm's-length: drop freehold houses sold under 75% of their CV.
        tenure = df.get("type_of_title")
        if tenure is not None:
            fh = tenure.astype(str).str.lower().str.contains("freehold")
            bad = fh & (df.get("cv_numeric") > 0) & (df["sale_price"] < 0.75 * df["cv_numeric"])
            df = df[~bad].copy()

        # Pre-compute each comp's v4 (hedonic) value once.
        df["comp_v4"] = [self._v4(r) for _, r in df.iterrows()]
        df = df.dropna(subset=["comp_v4"])
        df = df[df["comp_v4"] > 0]

        # ---- CV-ratio machinery (Matthew's method: value = CV × area sale/CV) ----
        # Each comp's own sale-to-CV ratio, guarded so a broken council CV can't
        # poison the median. Value = subject CV × the median of these ratios.
        cvr = df["sale_price"] / df["cv_numeric"]
        df["sale_cv"] = cvr.where(
            (df["cv_numeric"] > 0) & (cvr >= RATIO_CV_LO) & (cvr <= RATIO_CV_HI))
        df["tclass"] = (df["type_of_title"].map(_title_bucket)
                        if "type_of_title" in df.columns else "FH")

        self._by_sub = {k: g for k, g in df.groupby(["suburb", "ct"])}
        self._by_dist = {k: g for k, g in df.groupby(["district", "ct"])}

        # Fallback median sale/CV ratios (used when no tight comps match).
        vr = df.dropna(subset=["sale_cv"])
        # Ratios are keyed by TITLE CLASS as well as suburb and type: a freehold
        # is only ever compared with freehold sales, a cross-lease with
        # cross-lease, and so on. The tenures trade materially differently
        # against CV — freehold 0.968, cross-lease 0.953, unit title 0.929 —
        # so blending them values a unit title ~4% high and manufactures fake
        # margins on the deal list.
        self._ratio_sub = vr.groupby(["suburb", "ct"])["sale_cv"].median().to_dict()
        self._ratio_dist = vr.groupby(["district", "ct"])["sale_cv"].median().to_dict()
        self._ratio_type = vr.groupby("ct")["sale_cv"].median().to_dict()
        self._ratio_tclass = vr.groupby("tclass")["sale_cv"].median().to_dict()
        self._ratio_global = float(vr["sale_cv"].median()) if len(vr) else 1.0
        # Sample sizes behind each median. The ratio is shrunk toward the wider
        # area in proportion to how many sales stand behind it — a median over
        # three sales is mostly noise (see shrunk_cv_ratio).
        self._n_sub = vr.groupby(["suburb", "ct"]).size().to_dict()
        self._n_dist = vr.groupby(["district", "ct"]).size().to_dict()
        self._n_tclass = vr.groupby("tclass").size().to_dict()

        # Windowed suburb ratios: for each (suburb, type) take the most recent
        # window holding RATIO_MIN_COMPS sales. Falls through to the all-time
        # median (self._ratio_sub) when no window qualifies.
        self._ratio_sub_win: dict[tuple, float] = {}
        self._n_sub_win: dict[tuple, int] = {}
        if "_t" in vr.columns and vr["_t"].notna().any():
            t_max = float(vr["_t"].max())
            for key, g in vr.groupby(["suburb", "ct"]):
                for w in RATIO_WINDOWS_MONTHS:
                    m = g[g["_t"] > t_max - w]
                    if len(m) >= RATIO_MIN_COMPS:
                        self._ratio_sub_win[key] = float(m["sale_cv"].median())
                        self._n_sub_win[key] = int(len(m))
                        break

        # Title-segmented sale/CV: leasehold/cross-lease/unit-title sell below CV,
        # so a CV-based value must use the ratio for that title class (only keep
        # groups with enough sales to be meaningful).
        tt = vr.groupby(["ct", "tclass"])["sale_cv"]
        ttcnt = tt.size()
        self._ratio_type_t = {k: float(v) for k, v in tt.median().items() if ttcnt[k] >= 8}
        tc = vr.groupby("tclass")["sale_cv"]
        tccnt = tc.size()
        self._ratio_tcls = {k: float(v) for k, v in tc.median().items() if tccnt[k] >= 5}

        # Absolute sold price by (area, type, bed-count). Used to cap a CV-based
        # value: an inflated CV (e.g. new-build townhouses whose CV still reflects
        # the old site) can't claim a value above what same-size homes actually
        # sell for. Only kept where there are >=2 real sales.
        pv = df.copy()
        pv["bk"] = pd.to_numeric(pv["beds"], errors="coerce").round()
        pv = pv.dropna(subset=["bk"])

        def _price_tbl(keys):
            g = pv.groupby(keys)["sale_price"]
            med, cnt = g.median(), g.size()
            return {k: float(med[k]) for k in med.index if cnt[k] >= 2}

        self._abs_sub = _price_tbl(["suburb", "ct", "bk"])
        self._abs_dist = _price_tbl(["district", "ct", "bk"])

        # Floor $/m² — all-in sold price per m² of floor, by (suburb, type). This
        # is a SIZE-aware value: multiply by a listing's floor area to cap an
        # inflated-CV value. Unlike a bed-count cap it doesn't penalise large
        # homes (a big floor → a big cap). Built from sold data (~99% has floor).
        fr = df.copy()
        fr["fa"] = pd.to_numeric(fr.get("floor_area_m2"), errors="coerce")
        fr = fr[fr["fa"] > 10]
        fr["fpm2"] = fr["sale_price"] / fr["fa"]
        fr = fr[(fr["fpm2"] >= 1500) & (fr["fpm2"] <= 40000)]
        fs = fr.groupby(["suburb", "ct"])["fpm2"]
        fscnt = fs.size()
        self._floor_sub = {k: float(v) for k, v in fs.median().items() if fscnt[k] >= 4}
        ft = fr.groupby("ct")["fpm2"]
        ftcnt = ft.size()
        self._floor_type = {k: float(v) for k, v in ft.median().items() if ftcnt[k] >= 8}
        self._floor_global = float(fr["fpm2"].median()) if len(fr) else None

    @staticmethod
    def _v4(row) -> float | None:
        p = predict(
            suburb=row.get("suburb"), district=row.get("district"),
            property_type=row.get("property_type"), cv=row.get("cv_numeric"),
            floor=row.get("floor_area_m2"), land=row.get("land_area_m2"),
            beds=row.get("beds"), baths=row.get("baths"), cars=row.get("cars"),
            age=_age_from(row), title=row.get("type_of_title"),
            method=None, pool=False, address=row.get("address"),
        )
        return p.pred_v35

    def _cascade(self, *, suburb, district, ctype, beds, baths, land):
        """Return (tier, comps_df) for the tightest tier with 2+ comps, else (None, None)."""
        g = self._by_sub.get((suburb, ctype))
        has = lambda v: v is not None and v == v  # noqa: E731 (not NaN)
        if g is not None:
            if has(beds) and has(baths) and has(land):
                m = g[(g["beds"].sub(beds).abs() <= 1) & (g["baths"].sub(baths).abs() <= 1)
                      & (g["land_area_m2"].between(0.85 * land, 1.15 * land))]
                if len(m) >= MIN_COMPS:
                    return 1, m
            if has(beds) and has(land):
                m = g[(g["beds"].sub(beds).abs() <= 1) & (g["land_area_m2"].between(0.85 * land, 1.15 * land))]
                if len(m) >= MIN_COMPS:
                    return 2, m
            if has(beds):
                m = g[g["beds"].sub(beds).abs() <= 1]
                if len(m) >= MIN_COMPS:
                    return 3, m
            if len(g) >= MIN_COMPS:
                return 4, g
        gd = self._by_dist.get((district, ctype))
        if gd is not None:
            if has(beds):
                m = gd[gd["beds"].sub(beds).abs() <= 1]
                if len(m) >= MIN_COMPS:
                    return 5, m
            if len(gd) >= MIN_COMPS:
                return 6, gd
        return None, None

    def buy_price(self, *, suburb, district, property_type, beds, baths, land,
                  asking, v4_value, cv) -> BuyResult:
        try:
            cv = float(cv) if cv is not None and float(cv) == float(cv) else None
        except (TypeError, ValueError):
            cv = None
        if not v4_value or v4_value != v4_value or v4_value <= 0:  # catches None / NaN / ≤0
            # No model value. The asking price is what the property is actually
            # offered at, so it wins whenever it exists. CV is only an estimate of
            # the price and is used solely when there is no asking price — and
            # then as CV × what the area actually sells for vs CV, since raw CV is
            # a rating figure, not a market price.
            if asking and asking > 0:
                return BuyResult(round(DISCOUNT * float(asking)), round(float(asking)), None, 0)
            if cv and cv > 0:
                ratio, _src = self.cv_ratio_for(
                    suburb=suburb, district=district, property_type=property_type,
                    beds=beds, baths=baths, land=land,
                )
                base = cv * float(ratio or 1.0)
                return BuyResult(round(DISCOUNT * base), round(base), None, 0)
            return BuyResult(None, None, None, 0)

        ctype = canonical_type(property_type)

        def _num(x):
            try:
                return float(x) if x is not None and float(x) == float(x) else None
            except (TypeError, ValueError):
                return None

        tier, comps = self._cascade(
            suburb=suburb, district=district, ctype=ctype,
            beds=_num(beds), baths=_num(baths), land=_num(land),
        )

        if comps is not None:
            ratios = (comps["sale_price"] / comps["comp_v4"])
            ratios = ratios[(ratios >= RATIO_LO) & (ratios <= RATIO_HI)]
            area = v4_value * float(ratios.mean()) if len(ratios) else v4_value
            n = int(len(ratios))
        else:
            area = v4_value     # v4-fallback (already local-data based)
            n = 0
            tier = None

        # We value off what recent local sales say a property is worth — NOT what
        # a vendor listed it at (a list price is a marketing floor to draw bids;
        # these homes sell above it). So the buy price is a discount off the
        # sales-based value, and the list price never enters here.
        #
        # Sanity check uses that same sales figure as its reference: if the comp
        # cascade lands well below CV × the area's recent sale/CV ratio, the comps
        # are thin/unreliable, so fall back to the CV-ratio value (24 Macbeth Court
        # modelled at $1.73M on thin comps against a $3.5M CV — the ratio catches
        # it without ever looking at the asking price).
        if cv and cv > 0:
            ratio, _src = self.cv_ratio_for(
                suburb=suburb, district=district, property_type=property_type,
                beds=beds, baths=baths, land=land,
            )
            if ratio and ratio > 0 and area < (1 - MODEL_VS_ASKING_MAX_GAP) * cv * float(ratio):
                area = cv * float(ratio)

        # buy_price = 0.95 × MIN(asking, area_value) — NEVER above asking (spec §4).
        # area_value stays the pure "what it's worth" figure; only the acquisition
        # price is clamped to the list. Without this a listing whose CV-ratio value
        # sits well above a low list price (common on subdivision sites: land worth
        # far more than the vendor's asking) booked a buy price ABOVE the list — a
        # $3.0M listing showed a $4.27M buy. The subdivision UPSIDE belongs in
        # subdivision_profit, not in the acquisition price.
        try:
            ask = float(asking) if asking is not None and float(asking) == float(asking) else None
        except (TypeError, ValueError):
            ask = None
        buy_base = min(area, ask) if (ask and ask > 0) else area
        buy = DISCOUNT * buy_base
        return BuyResult(round(buy), round(area), tier, n)

    def cv_ratio_for(self, *, suburb, district, property_type, beds=None,
                     baths=None, land=None, title=None):
        """Sale/CV ratio for this listing — value = subject CV x this ratio.

        Delegates to shrunk_cv_ratio: what the same TYPE of house has been
        selling for against CV in that AREA, weighted by how many sales stand
        behind it.

        This replaced a tight comp cascade (same suburb + beds + baths + land
        +/-15%). That cascade was measurably worse than quoting the raw council
        CV — 10.06% vs 9.39% median error across 5 holdout splits — because it
        routinely resolved down to 2-5 sales and reported their noise as signal.
        The broad, shrunk estimate scores 8.99% and beats both on every split.

        beds/baths/land/title are accepted and ignored: kept so existing callers
        keep working, and to record that they were tested and did not help.
        Title moves the ratio by only 1-2% in this data (CL 0.994, UT 0.984),
        and segmenting by it made unit titles worse.
        """
        return self.shrunk_cv_ratio(
            suburb=suburb, district=district, property_type=property_type, title=title)

    def shrunk_cv_ratio(self, *, suburb, district, property_type,
                        title=None) -> tuple[float, str]:
        """Sale/CV ratio for this area and property type, shrunk by sample size.

        What the same type of house has been selling for against CV in that
        area. Deliberately a BROAD grouping: Auckland transacts at ~0.97x CV
        almost everywhere, so the ratio barely varies street to street, and
        resolving it finely measures sampling noise rather than the market.

        A suburb's own median is trusted in proportion to how many sales stand
        behind it — with K_SUBURB sales it carries half the weight, the rest
        coming from the district (itself shrunk toward the global median).

        Returns (ratio, source); source records how local the estimate really is.
        """
        ctype = canonical_type(property_type)
        # Comps are POOLED across tenure, deliberately. Splitting them by title
        # was measured at 7.67% against 7.56% pooled — the CV already prices the
        # tenure (a leasehold's council valuation reflects that it is leasehold),
        # so sale/CV barely differs by title once broken CVs are excluded:
        # CL 0.994, FH 1.003, UT 0.990. Splitting costs more in sample size than
        # it recovers in bias. Leasehold is the exception — only 41 sold comps
        # nationally — and those listings are kept off the deal list instead.
        g = self._ratio_global

        dk = (district, ctype)
        d_med, d_n = self._ratio_dist.get(dk), self._n_dist.get(dk, 0)
        if d_med is not None and d_n > 0:
            base = (d_n * float(d_med) + K_DISTRICT * g) / (d_n + K_DISTRICT)
            src = "district"
        else:
            base, src = g, "global"

        sk = (suburb, ctype)
        # Prefer the windowed ratio (recent sales only); fall back to all-time.
        s_med = self._ratio_sub_win.get(sk, self._ratio_sub.get(sk))
        s_n = self._n_sub_win.get(sk, self._n_sub.get(sk, 0))
        if s_med is not None and s_n > 0:
            ratio = (s_n * float(s_med) + K_SUBURB * base) / (s_n + K_SUBURB)
            src = "suburb" if s_n >= K_SUBURB else f"suburb_shrunk_n{s_n}"
        else:
            ratio = base
        return float(ratio), src

    def spec_value(self, *, suburb, district, property_type, beds, baths,
                   land, floor, cv) -> tuple[float | None, str | None, int]:
        """Value = this property's CV x what LIKE-FOR-LIKE sales went for vs CV.

        A comparable is: same suburb, same property type, same bed count, same
        bath count, land within SPEC_TOL and floor within SPEC_TOL. Their
        sale/CV ratios are taken (outliers outside 0.75-1.25 dropped, per the v4
        tool) and the median applied to this property's CV.

        Measured on 5 holdout splits of 2026 sales, against ACTUAL SOLD PRICES:
            this method, min 3 comps      6.94%   (fires on 14%)
            this method, min 2 comps      7.03%   (fires on 22%)
            CV x suburb/type area ratio   7.55%   (fires on 100%)
        Tighter beats broader here because matching beds+baths+size finds the
        same product; the area ratio averages a whole suburb together.

        Cascades outward only when a tier cannot be filled:
          1. beds + baths + land + floor    (the spec)
          2. beds + baths + floor           (no land recorded)
          3. beds + baths                   (no size recorded)
        Returns (value, tier, n). None when even tier 3 is too thin — the caller
        then falls back to the shrunk area ratio.
        """
        ctype = canonical_type(property_type)

        def _n(x):
            try:
                v = float(x)
                return v if v == v else None
            except (TypeError, ValueError):
                return None

        b, ba, la, fl = _n(beds), _n(baths), _n(land), _n(floor)
        cvv = _n(cv)
        if not cvv or cvv <= 0:
            return None, None, 0

        g = self._by_sub.get((str(suburb).strip(), ctype))
        if g is None or g.empty:
            return None, None, 0
        base = g
        if b is not None:
            base = base[base["beds"] == b]
        if ba is not None:
            base = base[base["baths"] == ba]
        if base.empty:
            return None, None, 0

        tiers = []
        if la and fl:
            tiers.append(("land_floor", base[
                base["land_area_m2"].between((1 - SPEC_TOL) * la, (1 + SPEC_TOL) * la)
                & base["floor_area_m2"].between((1 - SPEC_TOL) * fl, (1 + SPEC_TOL) * fl)]))
        if fl:
            tiers.append(("floor", base[base["floor_area_m2"].between(
                (1 - SPEC_TOL) * fl, (1 + SPEC_TOL) * fl)]))
        tiers.append(("beds_baths", base))

        for tier, m in tiers:
            rr = m["sale_cv"].dropna()
            rr = rr[(rr >= SPEC_RATIO_LO) & (rr <= SPEC_RATIO_HI)]
            if len(rr) >= SPEC_MIN_COMPS:
                return float(cvv * rr.median()), tier, int(len(rr))
        return None, None, 0

    def matched_sold_price(self, *, suburb, district, property_type,
                           beds, baths, land, floor) -> tuple[float | None, str | None, int]:
        """Median sold price of genuinely like-for-like sales. No CV anywhere.

        Same suburb, same type, same bed count, same bath count, with land and
        floor within MATCH_TOL. For a listing whose council record is incomplete
        (CV = land value, buildings unassessed) every CV-anchored method is
        poisoned, so this values it purely from what comparable buildings in that
        area actually sold for.

        Tiers, tightest first — returns (price, tier, n):
          1. beds + baths + land +/-20% + floor +/-20%
          2. beds + baths + floor +/-20%          (no land recorded)
          3. beds + baths                          (no size recorded)
          4. district-wide beds + baths
        Less accurate than the CV path (~11.5% vs ~7.7% measured) but it is the
        only honest option when the CV cannot be trusted.
        """
        ctype = canonical_type(property_type)

        def _n(x):
            try:
                v = float(x)
                return v if v == v else None
            except (TypeError, ValueError):
                return None

        b, ba = _n(beds), _n(baths)
        la, fl = _n(land), _n(floor)
        if b is None or ba is None:
            return None, None, 0

        for scope, key in (("suburb", (str(suburb).strip(), ctype)),
                           ("district", (str(district).strip(), ctype))):
            g = (self._by_sub if scope == "suburb" else self._by_dist).get(key)
            if g is None or g.empty:
                continue
            base = g[(g["beds"] == b) & (g["baths"] == ba) & (g["sale_price"] > 0)]
            if base.empty:
                continue
            attempts = []
            if la and fl:
                attempts.append(("land_floor", base[
                    base["land_area_m2"].between((1 - MATCH_TOL) * la, (1 + MATCH_TOL) * la)
                    & base["floor_area_m2"].between((1 - MATCH_TOL) * fl, (1 + MATCH_TOL) * fl)]))
            if fl:
                attempts.append(("floor", base[base["floor_area_m2"].between(
                    (1 - MATCH_TOL) * fl, (1 + MATCH_TOL) * fl)]))
            attempts.append(("beds_baths", base))
            for tier, m in attempts:
                if len(m) >= MATCH_MIN_COMPS:
                    return float(m["sale_price"].median()), f"{scope}_{tier}", int(len(m))
        return None, None, 0

    def sold_price_cap(self, *, suburb, district, property_type, beds):
        """Median absolute sold price of same-suburb (else district) comps of the
        same type AND bed-count. Caps a CV-based value so an inflated CV can't
        invent a deal. None when the segment is too thin to trust."""
        try:
            bk = round(float(beds))
        except (TypeError, ValueError):
            return None
        ctype = canonical_type(property_type)
        v = self._abs_sub.get((suburb, ctype, bk))
        if v is None:
            v = self._abs_dist.get((district, ctype, bk))
        return v

    def floor_rate_for(self, *, suburb, property_type):
        """Median all-in sold $/m² of floor for the suburb+type (else type, else
        global). Multiply by a listing's floor area for a size-aware value. None
        if we can't get a rate."""
        ctype = canonical_type(property_type)
        v = self._floor_sub.get((suburb, ctype))
        if v is None:
            v = self._floor_type.get(ctype)
        if v is None:
            v = self._floor_global
        return v
