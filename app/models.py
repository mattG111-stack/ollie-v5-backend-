from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEACTIVATED = "deactivated"


class BatchType(str, Enum):
    FOR_SALE = "for_sale"
    SOLD = "sold"
    RENT = "rent"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER.value, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.PENDING.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Links this user to their Stripe customer so the admin dashboard can read
    # their subscription/revenue. Set when a subscription is created.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- self-serve onboarding -------------------------------------------------
    # A self-serve signup walks: verify email → verify phone → add card → trial.
    # Product access is granted once they're trialing/paying (see security.
    # has_product_access); admins and admin-approved users bypass this.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cached from Stripe via webhook: 'trialing' | 'active' | 'past_due' |
    # 'canceled' | 'incomplete' | None (never subscribed).
    subscription_status: Mapped[str | None] = mapped_column(String(24), index=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signup_source: Mapped[str | None] = mapped_column(String(16))   # 'self' | 'admin'

    # --- assistant credentials ---------------------------------------------
    # The user's own LLM key, encrypted at rest (see assistant/keys.py). There
    # is no read path back to the plaintext through the API — only "is it set"
    # and the last four characters.
    llm_provider: Mapped[str | None] = mapped_column(String(16))
    llm_api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    llm_key_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Admin-provisioned key: the assistant works but the user can't see, change,
    # or remove the key, and the bring-your-own-key panel is hidden from them.
    llm_key_managed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WishList(Base):
    """A user's saved search / watch list. Matched against the active for-sale
    batch; new matches (vs the batch the user last viewed) drive the in-app
    notification badge."""
    __tablename__ = "wish_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # criteria (all optional — an empty wish list matches everything)
    district: Mapped[str | None] = mapped_column(String(64))
    suburb: Mapped[str | None] = mapped_column(String(120))
    property_category: Mapped[str | None] = mapped_column(String(24))   # house/townhouse/...
    min_price: Mapped[float | None] = mapped_column(Float)
    max_price: Mapped[float | None] = mapped_column(Float)
    min_beds: Mapped[int | None] = mapped_column(Integer)
    underpriced_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    subdividable_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # "developments under a value": subdividable sites whose buy price is <= this
    max_dev_buy_price: Mapped[float | None] = mapped_column(Float)
    # the for-sale batch the user last viewed these matches against
    last_seen_batch_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_type: Mapped[str] = mapped_column(String(16), nullable=False)
    region: Mapped[str] = mapped_column(String(64), default="Auckland", nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Two-stage publish lifecycle: a batch is ingested as "staged" (loaded but not
    # live), reviewed, then "published" (is_active flips true, old one archived).
    #   staged → published → archived
    status: Mapped[str] = mapped_column(String(16), default="staged", nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)


class IngestJob(Base):
    """Tracks a single async ingest task. Created the moment a file lands;
    background task updates status as it progresses."""
    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_type: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str | None] = mapped_column(String(1024))  # temp path on disk
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)  # pending|running|completed|failed
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    # `stage` is a SHORT human-readable label ONLY — "load", "enrich", "price",
    # "publish", "done", "error". It is varchar(64); never serialise a result dict
    # into it (that raised StringDataRightTruncation when the publish result was
    # written here). Structured results belong in result_json below.
    stage: Mapped[str | None] = mapped_column(String(64))  # human-readable current stage
    rows_total: Mapped[int | None] = mapped_column(Integer)
    rows_inserted: Mapped[int | None] = mapped_column(Integer)
    rows_rejected: Mapped[int | None] = mapped_column(Integer)
    # Durable enrich progress: CoreLogic returns nothing for many addresses, which
    # is a normal outcome, not a failure — filled vs missed must be distinguishable
    # and must survive a page refresh / browser disconnect (progress lives in the
    # DB, not the tab). rows_filled = cells filled; rows_missed = lookups that
    # returned nothing.
    rows_filled: Mapped[int | None] = mapped_column(Integer, default=0)
    rows_missed: Mapped[int | None] = mapped_column(Integer, default=0)
    # Structured result of a stage (e.g. the publish result dict), as JSON text.
    # Keeps payloads out of the short `stage` label.
    result_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # JSON-encoded list of anomaly warnings found by the post-ingest audit
    # (e.g. extreme market values, suspicious correction lookups). Distinct
    # from error_message: warnings don't fail the ingest, they flag rows
    # for human review on the admin uploads page.
    audit_warnings: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Shared columns the scraper produces for every property listing.
class _PropertyMixin:
    address: Mapped[str | None] = mapped_column(String(512))
    name: Mapped[str | None] = mapped_column(String(512))

    suburb: Mapped[str | None] = mapped_column(String(128), index=True)
    district: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(64), default="Auckland", index=True)
    postcode: Mapped[str | None] = mapped_column(String(16))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    property_type: Mapped[str | None] = mapped_column(String(128))
    type_of_title: Mapped[str | None] = mapped_column(Text)
    zoning: Mapped[str | None] = mapped_column(String(128), index=True)
    land_slope_contour: Mapped[str | None] = mapped_column(String(128))

    beds: Mapped[int | None] = mapped_column(Integer)
    baths: Mapped[int | None] = mapped_column(Integer)
    cars: Mapped[int | None] = mapped_column(Integer)
    floor_area_m2: Mapped[float | None] = mapped_column(Float)
    land_area_m2: Mapped[float | None] = mapped_column(Float)

    cv_numeric: Mapped[float | None] = mapped_column(Float)
    land_value_numeric: Mapped[float | None] = mapped_column(Float)
    improvement_value_numeric: Mapped[float | None] = mapped_column(Float)

    url: Mapped[str | None] = mapped_column(String(1024))
    slug_id: Mapped[str | None] = mapped_column(String(128), index=True)
    image_url: Mapped[str | None] = mapped_column(String(1024))
    image_count: Mapped[int | None] = mapped_column(Integer)
    # Every listing photo, newline-separated in scrape order. The scrape carries
    # up to 20; storing only the first meant the card and detail page could never
    # show a gallery.
    image_urls: Mapped[str | None] = mapped_column(Text)

    listing_date: Mapped[str | None] = mapped_column(String(32))
    days_on_market: Mapped[float | None] = mapped_column(Float)

    # === Raw scraper fields we preserve for future use ===
    key_facts: Mapped[str | None] = mapped_column(Text)
    key_time_on_market: Mapped[str | None] = mapped_column(String(64))
    estate_description: Mapped[str | None] = mapped_column(Text)
    council_valuation_summary: Mapped[str | None] = mapped_column(Text)
    property_trend: Mapped[str | None] = mapped_column(Text)
    sale_status: Mapped[str | None] = mapped_column(String(32))
    last_updated: Mapped[str | None] = mapped_column(String(32))

    # === Third-party reference valuation (legacy column names retained for DB compat) ===
    third_party_valuation: Mapped[float | None] = mapped_column("hg_valuation", Float)
    third_party_valuation_high: Mapped[float | None] = mapped_column("hg_valuation_high", Float)
    third_party_valuation_low: Mapped[float | None] = mapped_column("hg_valuation_low", Float)
    valuation_last_date: Mapped[str | None] = mapped_column(String(32))

    # === CV change tracking ===
    valuation_rateable_change_pct: Mapped[float | None] = mapped_column(Float)
    valuation_land_change_pct: Mapped[float | None] = mapped_column(Float)
    valuation_improvement_change_pct: Mapped[float | None] = mapped_column(Float)

    # === Last sale of this property ===
    valuation_last_sold_value: Mapped[float | None] = mapped_column(Float)
    valuation_last_sold_date: Mapped[str | None] = mapped_column(String(32))
    sold_listing_date: Mapped[str | None] = mapped_column(String(32))
    sold_listing_price_label: Mapped[str | None] = mapped_column(String(128))

    # === Trend JSONs — for charts ===
    valuation_trend_yearly_json: Mapped[str | None] = mapped_column(Text)
    valuation_trend_monthly_json: Mapped[str | None] = mapped_column(Text)
    sale_history_json: Mapped[str | None] = mapped_column(Text)
    cv_history_json: Mapped[str | None] = mapped_column(Text)
    schools_json: Mapped[str | None] = mapped_column(Text)

    # === Agent contact ===
    agent1_name: Mapped[str | None] = mapped_column(String(255))
    agent1_phone: Mapped[str | None] = mapped_column(String(64))
    agent1_email: Mapped[str | None] = mapped_column(String(255))
    agent1_job_title: Mapped[str | None] = mapped_column(String(128))
    agent1_company_name: Mapped[str | None] = mapped_column(String(255))
    agent2_name: Mapped[str | None] = mapped_column(String(255))
    agent2_phone: Mapped[str | None] = mapped_column(String(64))
    agent2_email: Mapped[str | None] = mapped_column(String(255))
    agent2_job_title: Mapped[str | None] = mapped_column(String(128))
    agent2_company_name: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))

    # === Other potentially-present scraper fields ===
    building_age: Mapped[str | None] = mapped_column(String(32))
    parking_covered: Mapped[int | None] = mapped_column(Integer)
    parking_other: Mapped[int | None] = mapped_column(Integer)
    has_swimming_pool: Mapped[bool | None] = mapped_column(Boolean)
    is_new_construction: Mapped[bool | None] = mapped_column(Boolean)
    is_coastal_waterfront: Mapped[bool | None] = mapped_column(Boolean)
    storey_count: Mapped[int | None] = mapped_column(Integer)
    other_features: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    listing_title: Mapped[str | None] = mapped_column(String(512))
    listing_published_date: Mapped[str | None] = mapped_column(String(32))


class PropertyForSale(_PropertyMixin, Base):
    __tablename__ = "properties_for_sale"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # raw scrape
    asking_price: Mapped[float | None] = mapped_column(Float)

    # v3.8 model output (market_value = pred_v38 rounded to nearest $1k)
    market_value: Mapped[float | None] = mapped_column(Float)
    predicted_list: Mapped[float | None] = mapped_column(Float)
    predicted_days: Mapped[float | None] = mapped_column(Float)
    comps_used: Mapped[int | None] = mapped_column(Integer)  # legacy; now stores n_subtype
    confidence: Mapped[str | None] = mapped_column(String(16))
    pred_vs_cv: Mapped[float | None] = mapped_column(Float)
    pred_vs_listing: Mapped[float | None] = mapped_column(Float)
    # v3.8 diagnostic fields — explain how the estimate was computed
    pred_v35: Mapped[float | None] = mapped_column(Float)
    pred_v38: Mapped[float | None] = mapped_column(Float)
    z_weight: Mapped[float | None] = mapped_column(Float)
    beta_tier: Mapped[str | None] = mapped_column(String(16))
    cv_anchor: Mapped[float | None] = mapped_column(Float)
    cv_ratio_tier: Mapped[str | None] = mapped_column(String(16))
    correction_used: Mapped[str | None] = mapped_column(String(16))

    # === v4 production AVM (June 2026) ===
    # Two-tier: asking × 0.95 when usable, else v3.5 hedonic fallback.
    listing_type: Mapped[str | None] = mapped_column(String(16))      # fixed | auction | tender | negotiation | unknown
    pricing_path: Mapped[str | None] = mapped_column(String(16))      # asking | v35 | insufficient
    range_low: Mapped[float | None] = mapped_column(Float)            # honest price range — low end
    range_high: Mapped[float | None] = mapped_column(Float)           # honest price range — high end
    subdivision_premium: Mapped[float | None] = mapped_column(Float)  # separate line item, NOT folded into market_value
    # Independent fair value (CV-bounded hedonic) + margin vs asking for deal-finding.
    fair_value: Mapped[float | None] = mapped_column(Float)
    margin: Mapped[float | None] = mapped_column(Float)               # (fair_value/asking - 1); positive = potential deal
    is_premium: Mapped[bool | None] = mapped_column(Boolean, default=False)  # ultra-prime: priced off listing, no model valuation/deal

    # === Acquisition layer (buy price) — cascade comps, June 2026 spec ===
    buy_price: Mapped[float | None] = mapped_column(Float)            # 0.95 × MIN(asking, area_value)
    area_value: Mapped[float | None] = mapped_column(Float)           # v4 × avg(comp_sale / comp_v4)
    comp_tier: Mapped[int | None] = mapped_column(Integer)            # 1–6 (tightest tier with 2+ comps); NULL = v4 fallback
    comps_matched: Mapped[int | None] = mapped_column(Integer)        # number of comps used

    # === Subdivision (§6 section-rate model) ===
    sections: Mapped[int | None] = mapped_column(Integer)             # number of sections the site splits into
    dwellings: Mapped[int | None] = mapped_column(Integer)            # sections × max dwellings per lot
    section_rate: Mapped[float | None] = mapped_column(Float)         # $/m² (from sold land values)
    gross_sales: Mapped[float | None] = mapped_column(Float)          # sections × section value
    subdivision_profit: Mapped[float | None] = mapped_column(Float)   # gross sales − buy − costs

    # subdivision
    min_lot_m2: Mapped[float | None] = mapped_column(Float)
    max_addl_lots: Mapped[float | None] = mapped_column(Float)
    section_price_per_m2: Mapped[float | None] = mapped_column(Float)
    section_value_method: Mapped[str | None] = mapped_column(String(32))
    services_cost: Mapped[float | None] = mapped_column(Float)
    total_subdivided_value: Mapped[float | None] = mapped_column(Float)
    uplift_vs_asking: Mapped[float | None] = mapped_column(Float)

    # cashflow
    est_weekly_rent: Mapped[float | None] = mapped_column(Float)
    est_gross_yield: Mapped[float | None] = mapped_column(Float)
    annual_gross_rent: Mapped[float | None] = mapped_column(Float)
    annual_net_rent: Mapped[float | None] = mapped_column(Float)
    annual_mortgage: Mapped[float | None] = mapped_column(Float)
    annual_cashflow: Mapped[float | None] = mapped_column(Float)
    cash_on_cash: Mapped[float | None] = mapped_column(Float)
    breakeven_deposit_pct: Mapped[float | None] = mapped_column(Float, index=True)
    # What this will transact at (asking x 0.95 when listed, else CV x area ratio).
    # Distinct from fair_value, which is deliberately computed WITHOUT the asking
    # price so the margin means something.
    expected_sale: Mapped[float | None] = mapped_column(Float, index=True)
    expected_sale_path: Mapped[str | None] = mapped_column(String(32))
    expected_sale_band: Mapped[float | None] = mapped_column(Float)

    # scoring
    opportunity_score: Mapped[float | None] = mapped_column(Float, index=True)
    opportunity_score_pct: Mapped[float | None] = mapped_column(Float, index=True)  # 0-100 normalized
    best_strategy: Mapped[str | None] = mapped_column(String(32))
    best_net_gain: Mapped[float | None] = mapped_column(Float)
    is_underpriced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_cashflow_positive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_subdividable: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Weekly pre-publish verification against the listing's own page (app.verify):
    # the land area the source shows, and a verdict — "ok" | "mismatch" |
    # "unverified" (couldn't read the page). A mismatch means the stored land area
    # is wrong (e.g. 139 Long Drive stored 5,665 m² vs 416 m² on the listing) and
    # the area-dependent signals (subdivision) are held back for it.
    land_area_listing_m2: Mapped[float | None] = mapped_column(Float)
    land_area_flag: Mapped[str | None] = mapped_column(String(16), index=True)

    # On-demand external estimate (homes.co.nz), cached per property so it's
    # fetched once, not every page view. See app.external_estimates.
    homes_valuation: Mapped[float | None] = mapped_column(Float)
    homes_valuation_low: Mapped[float | None] = mapped_column(Float)
    homes_valuation_high: Mapped[float | None] = mapped_column(Float)
    homes_cv: Mapped[float | None] = mapped_column(Float)
    homes_url: Mapped[str | None] = mapped_column(String(300))
    homes_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # realestate.co.nz — slot reserved; populated once a data source is wired in.
    realestate_valuation: Mapped[float | None] = mapped_column(Float)
    realestate_valuation_low: Mapped[float | None] = mapped_column(Float)
    realestate_valuation_high: Mapped[float | None] = mapped_column(Float)
    realestate_url: Mapped[str | None] = mapped_column(String(300))

    # propertyvalue.co.nz (CoreLogic) — on-demand, cached. Flat fields feed the
    # Compare tile; pv_data holds the full record (attrs/zoning/CV/sale) that the
    # verification cross-check compares against our own data. See app.propertyvalue.
    pv_estimate_low: Mapped[float | None] = mapped_column(Float)
    pv_estimate_high: Mapped[float | None] = mapped_column(Float)
    pv_estimate_mid: Mapped[float | None] = mapped_column(Float)
    pv_cv: Mapped[float | None] = mapped_column(Float)
    pv_url: Mapped[str | None] = mapped_column(String(400))
    # CoreLogic's last recorded sale (their data, kept separate from our own
    # valuation_last_sold_* which the pipeline owns). Grabbed for every deal.
    pv_last_sale_price: Mapped[float | None] = mapped_column(Float)
    pv_last_sale_date: Mapped[str | None] = mapped_column(String(32))
    pv_data: Mapped[str | None] = mapped_column(Text)          # JSON of pv_lookup()
    pv_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Two-stage publish: a row flagged during the pre-publish review is HELD back —
    # its batch can go live while this row stays hidden until an admin fixes it and
    # publishes it individually. Held rows are excluded from all live views.
    is_held: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    hold_reason: Mapped[str | None] = mapped_column(String(300))

    # CV reconciliation (scripts/reconcile_cv): when our CV and homes' CV disagree,
    # the market (asking vs CV × the suburb sale ratio) decides which is credible.
    # "ok" = our CV is backed; "suspect" = the market sides with homes' CV, so our
    # CV — and any deal signal it drove — is untrustworthy for this listing.
    cv_flag: Mapped[str | None] = mapped_column(String(16), index=True)

    __table_args__ = (
        Index("ix_fs_active_suburb", "import_batch_id", "suburb"),
        Index("ix_fs_active_score", "import_batch_id", "opportunity_score_pct"),
    )


class PropertySold(_PropertyMixin, Base):
    __tablename__ = "properties_sold"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sale_price: Mapped[float | None] = mapped_column(Float, index=True)
    sold_date: Mapped[str | None] = mapped_column(String(32), index=True)
    sale_method: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_sold_comp", "import_batch_id", "suburb", "beds", "baths"),
        Index("ix_sold_zoning", "import_batch_id", "zoning"),
    )


class PropertyRent(_PropertyMixin, Base):
    __tablename__ = "properties_rent"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    weekly_rent: Mapped[float | None] = mapped_column(Float, index=True)
    listing_date_rent: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_rent_comp", "import_batch_id", "suburb", "beds"),
    )


class AgentContact(Base):
    """A user reaching out to Ollie's buyer's agent about a property — logged so
    the admin dashboard can count enquiries. One row per contact action."""
    __tablename__ = "agent_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    property_id: Mapped[int | None] = mapped_column(index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    suburb: Mapped[str | None] = mapped_column(String(120))
    channel: Mapped[str | None] = mapped_column(String(16))   # "email" | "phone"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class VerificationCode(Base):
    """A short-lived code sent to a user's email or phone during onboarding.
    One row per issued code; the newest unconsumed, unexpired row for a channel
    is the valid one. Codes are single-use (consumed_at) with an attempt cap."""
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)   # "email" | "phone"
    code: Mapped[str] = mapped_column(String(12), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
