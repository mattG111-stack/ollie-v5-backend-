# Ingest rebuild: four operator-triggered stages

## The problem

`ingest_for_sale` does everything in one blocking job: parse CSV, CoreLogic
enrichment, then pricing. On a 138 MB for-sale file this times out.

`_fill_df_from_corelogic` sleeps 0.5s per lookup, capped at 5,000 lookups. That
is up to 42 minutes of sleeping before pricing even begins, inside a single
request-triggered job.

When it died at 30% there was no way to tell whether it was still running,
stalled, or dead. Progress lived in the browser, not the database.

Second problem: priced rows appear live without an explicit publish, so bad
valuations (+216%, +433%, +547% vs CV) reached users before anyone saw them.

## The target flow

Four stages. Each triggered by the operator, each independently re-runnable,
each inspectable before moving on.

    1. LOAD     raw rows into the grid. Fast. No external calls, no pricing.
    2. ENRICH   button runs CoreLogic. Progress visible. Re-runnable.
    3. PRICE    button runs valuations. Operator sees the numbers.
    4. PUBLISH  button pushes the batch live.

Restartability is the point. If CoreLogic dies at 60%, re-run that stage only,
not the whole 138 MB file. If pricing looks wrong, fix the code and re-price
without reloading.

Stage 3 is also where the bad valuations get caught. The operator sees them in
the grid and never publishes that batch.

## Durable progress

Progress must persist to the database, not the browser. The failure mode being
fixed is exactly this: the tab dropped and the state was unknowable.

Per stage, persisted:

- `rows_processed` / `rows_total` — so it can be seen moving
- `rows_filled` / `rows_missed` — CoreLogic returns nothing for many addresses.
  That is a normal outcome, not a failure, and must be distinguishable from one.
- terminal state — `completed` | `failed` | `cancelled`
- `started_at` / `finished_at` — distinguishes "still running" from "stopped an
  hour ago"

Must survive a page refresh and remain readable after the browser disconnects.

## What already exists

Check these before building anything new.

`app/routers/release.py` already documents this flow in its module docstring:

    upload (stages the data) -> GET /staged (review the flags)
    -> fix any held rows (PATCH) -> POST /publish (goes live)

Existing endpoints: `/release/staged`, `/release/held`, `/release/publish`,
`/listings/{id}/publish`.

Existing UI: `app/admin/publish/page.tsx`, with "Held for review" and
"Rejected at load" stats.

So the publish gate (stage 4) and the held-row review may be largely built. If
rows are appearing live without an explicit publish, check whether the read path
in `routers/properties.py` filters on the staged/active flag. That would make
stage 4 a wiring fix rather than a build.

The genuinely new work is splitting stages 1-3 apart and the durable progress.

## Open bugs to fix alongside

1. **Anchor guard not taking effect.** `ANCHOR_TOLERANCE = 0.40` at
   `pricing/pipeline.py:79`, guard at ~line 409. Property 23851: CV $610,000,
   valuation $3,945,000 (+546.7%), should be forced to ~$579,500. It is not
   firing. `RATIO_CAP = 1.10` is also being bypassed on the same row, so the
   value is not coming through the CV path. Find what sets or overwrites
   `ollie_value` after line 413, and confirm whether stored values are being
   served rather than recomputed.

2. **Buy price exceeds asking.** `pricing/buyprice.py:18` specifies
   "never above asking" and `pipeline.py:294` enforces it, but subdivision
   listings show a buy price above list (e.g. $4,267,000 against a $3,000,000
   list). Suspect the subdivision path at `pricing/subdivision.py:357`
   overwrites `buy_price` without passing back through the clamp.

3. **Two fields, not one.** "Estimated value" (what it is worth) and
   "Estimated buy price" (what you can pay) are different numbers and need
   separate labels. Currently one label does both jobs, which reads as an
   instruction to overpay.

4. **No CV fallback.** `app/external_estimates.py` and `app/propertyvalue.py`
   already exist and are wired into ingest. Use those estimates as the pricing
   anchor when `cv_v` is missing or the CV record is land-value-only, instead of
   falling through to the uncapped `elif bp.area_value` branch at
   `pipeline.py:386`.

5. **Accuracy tests do not run.** `tests/test_valuation_accuracy.py` hardcodes
   `/Users/matthewgrant/projects/ollie/data/1_sold_comps/sold_comps.csv`. Those
   8 tests skip silently on any other machine, so the guard against valuation
   drift is not actually running. Fix the path before trusting any accuracy
   claim about the changes above.
