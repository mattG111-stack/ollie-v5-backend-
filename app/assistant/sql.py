"""Let the model write its own read-only SQL, safely.

Seven hand-written tools can only answer the seven questions we thought of.
"Which suburb has the most 4-bed houses under $1.2M with land over 700m2" is a
perfectly reasonable question that no curated tool covers. Giving the model the
schema and letting it query is the only way to answer arbitrary questions.

That means the safety has to be real, not a keyword blocklist. Four layers:

1. READ ONLY TRANSACTION — Postgres itself rejects any write. Verified: an
   UPDATE inside one raises InternalError. This is the load-bearing control;
   string matching alone would be a sieve.
2. Table allowlist — a read-only transaction still permits `SELECT * FROM users`,
   which holds password hashes. Only the property tables are reachable.
3. statement_timeout — a runaway cross join can't wedge the connection pool.
4. Row cap — a LIMIT is appended when absent, so nothing returns 130k rows.

Anything rejected comes back to the model as an error string, so it can correct
its own query rather than the request failing.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import text

from ..db import engine

STATEMENT_TIMEOUT_MS = 10_000
MAX_ROWS = 200

# Only these are reachable. Everything else — users (password hashes),
# alembic_version, pg_catalog, information_schema — is rejected before execution.
ALLOWED_TABLES = {
    "properties_for_sale",
    "properties_sold",
    "properties_rent",
    "import_batches",
}

# Matches a table name after FROM / JOIN, with optional schema qualifier.
_TABLE_REF = re.compile(
    r"\b(?:from|join)\s+(?:only\s+)?([a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)?)",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Comments can hide a second statement or a blocked identifier."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


class UnsafeQuery(ValueError):
    """The query was rejected before it reached the database."""


def validate(sql: str) -> str:
    """Reject anything that isn't a single, read-only, allowlisted SELECT."""
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQuery("Empty query.")

    # One statement only — a trailing semicolon is fine, an embedded one is not.
    if ";" in cleaned:
        raise UnsafeQuery("Only one statement per query. Remove the semicolon.")

    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQuery("Only SELECT (or WITH ... SELECT) queries are allowed.")

    # CTE names are legitimate FROM targets; collect them so they aren't
    # mistaken for real tables.
    cte_names = {
        m.group(1).lower()
        for m in re.finditer(r"\b([a-zA-Z_][\w$]*)\s+as\s*\(", cleaned, re.IGNORECASE)
    }

    referenced = {m.group(1).lower() for m in _TABLE_REF.finditer(cleaned)}
    for ref in referenced:
        bare = ref.split(".")[-1]
        if bare in cte_names or ref in cte_names:
            continue
        if bare not in ALLOWED_TABLES:
            raise UnsafeQuery(
                f"Table '{ref}' is not queryable. Available tables: "
                + ", ".join(sorted(ALLOWED_TABLES))
            )

    if not _LIMIT.search(cleaned):
        cleaned = f"{cleaned} LIMIT {MAX_ROWS}"
    return cleaned


def run(sql: str) -> str:
    """Validate then execute inside a read-only, time-limited transaction."""
    try:
        safe = validate(sql)
    except UnsafeQuery as exc:
        return f"Query rejected: {exc}"

    try:
        with engine.connect() as conn:
            # Both statements must be inside the same transaction as the query.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            result = conn.execute(text(safe))
            cols = list(result.keys())
            rows = [dict(zip(cols, r)) for r in result.fetchmany(MAX_ROWS)]
    except Exception as exc:  # noqa: BLE001 - returned to the model to self-correct
        return f"Query failed: {type(exc).__name__}: {str(exc)[:400]}"

    return json.dumps(
        {
            "sql": safe,
            "row_count": len(rows),
            "truncated": len(rows) >= MAX_ROWS,
            "rows": rows,
        },
        default=str,
    )


def distinct_values(table: str, column: str, limit: int = 40) -> str:
    """Return the actual distinct values of a column, with counts.

    The single biggest cause of a wrong answer is the model guessing a value
    that isn't spelled the way the data stores it — 'Flatbush' vs 'Flat Bush',
    'House' vs the Chinese property_type. This lets it check first.
    """
    if table not in ALLOWED_TABLES:
        return f"Table '{table}' is not queryable."
    if not re.fullmatch(r"[a-zA-Z_][\w]*", column or ""):
        return "Invalid column name."
    # Scope to the active batch so the counts are honest, not summed across six
    # historical snapshots.
    bt = "sold" if table == "properties_sold" else "for_sale"
    batch = (
        "import_batch_id = (SELECT id FROM import_batches "
        f"WHERE is_active AND batch_type = '{bt}')"
        if table in ("properties_for_sale", "properties_sold") else "TRUE"
    )
    sql = (
        f"SELECT {column} AS value, COUNT(*) AS n FROM {table} "
        f"WHERE {batch} AND {column} IS NOT NULL GROUP BY {column} "
        f"ORDER BY n DESC LIMIT {min(int(limit), 100)}"
    )
    return run(sql)


# --- schema description handed to the model -------------------------------

SCHEMA = """You can query these Postgres tables directly with read-only SQL.

IMPORTANT — always filter to the active batch, or you will mix six historical
snapshots together and get inflated counts:
    WHERE import_batch_id = (SELECT id FROM import_batches
                             WHERE is_active AND batch_type = 'for_sale')
Use batch_type = 'sold' for properties_sold.

properties_for_sale — live listings (~10,900 in the active batch)
  id, address, suburb, district, region, postcode, latitude, longitude
  property_type, type_of_title, zoning, land_slope_contour
  beds, baths, cars, floor_area_m2, land_area_m2, building_age
  asking_price            what it is listed at
  cv_numeric              council valuation
  land_value_numeric, improvement_value_numeric
  fair_value              OUR valuation (the sold-data estimate)
  buy_price               what we'd pay
  margin                  (fair_value - asking_price) / asking_price, as a fraction
  pred_vs_cv              our value vs CV, as a fraction
  confidence              'low' | 'medium' | 'high'
  comps_used              how many sold comps backed the valuation
  range_low, range_high   likely sale range
  predicted_days, days_on_market
  est_weekly_rent, est_gross_yield, annual_cashflow, cash_on_cash
  breakeven_deposit_pct
  is_underpriced, is_cashflow_positive, is_subdividable   booleans
  max_addl_lots, min_lot_m2, total_subdivided_value, best_net_gain, best_strategy
  opportunity_score_pct
  has_swimming_pool, is_new_construction, is_coastal_waterfront
  url, image_url, image_urls, import_batch_id

properties_sold — completed sales, the evidence base
  id, address, suburb, district, property_type, type_of_title
  beds, baths, floor_area_m2, land_area_m2
  sale_price, cv_numeric, land_value_numeric
  sale_method   'A - Auction' | 'P - Private Treaty(Neg.)' | 'T - Tender'
  sold_date, days_on_market, has_swimming_pool, import_batch_id

import_batches — id, batch_type ('for_sale' | 'sold' | 'rent'), region, is_active, filename

Notes that will save you a wrong answer:
- margin and pred_vs_cv are FRACTIONS (0.15 = 15%), not percentages.
- asking_price is null on ~0.5% of listings; fair_value is null where we could
  not value it. Filter with IS NOT NULL when averaging.
- A sale_price / cv_numeric ratio outside 0.3-3.0 is a broken council record —
  exclude those when computing anything against CV.
- properties_sold has no is_underpriced / margin — those are for-sale concepts.

CATEGORICAL VALUES — these will trip you up if you guess. When a name or category
might not match exactly, call distinct_values(table, column) first, or use ILIKE.

- property_type is stored in CHINESE on for-sale listings. Never query
  property_type = 'House' — it returns nothing. The mapping:
      独立屋 = House (the vast majority)   城市屋 / 排房 = Townhouse
      公寓 = Apartment                     单元房 = Unit
      建地 / Residential - Vacant = Section  乡村别墅 = Lifestyle Property
      乡村住宅建地 = Lifestyle Section
  To count houses: WHERE property_type = '独立屋'. To count everything
  house-like, prefer filtering on beds/land rather than the raw type string.
- district has exactly 9 values: Auckland City, Franklin, Hauraki Gulf Islands,
  Manukau City, North Shore City, Papakura, Rodney, Waitakere City, Waiheke Island.
- suburb is free text — always match with ILIKE '%name%', never '='.
  ('Flat Bush', 'Browns Bay', 'Mount Albert' — two words, exact spelling matters.)
- type_of_title on properties_for_sale is a title REFERENCE NUMBER, not a
  category — do not group by it expecting Freehold/Leasehold.
- type_of_title on properties_sold is a numeric code: '1.0'=Freehold,
  '2.0'=Leasehold, '3.0'=Cross-Lease, '4.0'=Unit Title.
- sale_method on properties_sold: 'A - Auction', 'P - Private Treaty(Neg.)',
  'T - Tender'. Match with LIKE 'A -%' etc.
- zoning values are like 'Residential - Mixed Housing Suburban Zone',
  'Residential - Single House Zone'. Use ILIKE '%single house%' to match loosely."""
