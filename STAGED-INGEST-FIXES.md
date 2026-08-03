# Staged ingest: four fixes

The four-stage flow (load / enrich / price / publish) is built and works. A full
14,259-row batch ran end to end. These are the gaps found on that first real run.

---

## 1. Enrich stops short — raise or remove the lookup cap

**Symptom:** enrich reported "lookup cap of 5,000 reached" after 1h 29m, leaving
3,443 rows un-enriched on a 14,259-row batch. 75.9% done, not finished.

**Cause:** `app/ingest.py:388`

    def _fill_df_from_corelogic(df, *, delay: float = 0.5, cap: int = 5000)

The 5,000 cap predates resumability. It was a safety rail for a single blocking
job that could not be restarted; now that enrich checkpoints and resumes, the cap
just stops work early and forces a manual re-run.

**Fix:** raise the cap to comfortably exceed a full batch (15,000+), or drop it
and let resumability bound the work. Keep the circuit breaker — the
40-consecutive-miss rate-limit guard is doing a different and still useful job.

**Also:** the trigger condition only fires when floor OR land is blank:

    if not (_blank(row.get("key_floor_area")) or _blank(row.get("key_land_area"))):
        continue

A row with floor and land present but a blank CV is skipped, even though CV is
the primary pricing anchor (`ollie_value = cv_v * min(ratio, RATIO_CAP)`).
Widen the trigger to include blank `cv_numeric`.

---

## 2. Container restarts during enrich

**Symptom:** enrich crashed mid-run and resumed. Resumability worked — no data
lost — but each restart costs wall-clock time on a 90-minute stage.

**Cause:** the job blocks the request process for over an hour. `/health` stops
responding, Railway concludes the service is dead and replaces the container.
Memory was well under the cap, so this is not OOM. No traceback appears because
the process is killed, not raised.

**Fix:** run enrich as a background worker that does not block request handling,
so the healthcheck keeps answering while the stage runs.

---

## 3. Publish fails on a status write

**Symptom:**

    DataError: (psycopg.errors.StringDataRightTruncation)
    value too long for type character varying(64)
    'stage': "published: {'published': [{'batch_type': 'for_sale', 'batch_id': 9}], 'cou...

**Cause:** `app/models.py:139`

    stage: Mapped[str | None] = mapped_column(String(64))  # human-readable current stage

The publish step serialises a whole result dict into it. `stage` is a label —
"load", "enrich", "price", "publish" — not a payload.

**Note:** the publish itself appears to succeed; only the status update after it
fails. Confirm whether the batch actually went live before treating this as
blocking.

**Fix:** put the result in its own JSON column (or the existing counters) and
keep `stage` as a short label. Truncating at 64 chars is the quick patch but
leaves the wrong data in the wrong column.

---

## 4. Row grid and CSV export on the staged page

**This item spans both repos.** Backend first: the API must return the staged
rows with all four profit figures before the frontend can display them. Check
whether subdivision profit, gross realisation and development cost are exposed
on the properties response at all — the pipeline computes them (they drive the
buy ceiling) but they may not be serialised out.

The filter chips exist — All rows / Held / Unpriced / Not enriched / CoreLogic
missed — but there is no table beneath them, so a batch cannot actually be
inspected before publish. That was the whole point of staging.

**Add a sortable row grid** showing, per row:

    address, suburb, asking, CV, valuation, vs CV %,
    margin $, margin %,
    subdivision profit $, subdivision profit %, gross realisation,
    development cost, lots, buy score,
    floor area, land area, comps used, confidence

Must respect the active filter chip. Gross realisation and development cost are
the inputs behind the subdivision profit figures — shown so the number can be
sanity-checked rather than taken on trust.

**Four distinct numbers, four sortable columns.** These are not variants of
each other and must not be collapsed:

1. **Valuation** — what the property is worth as-is. The standalone value.

2. **Margin** — `ollie_value - asking`. What the listing is underpriced by on a
   straight buy. This is the deal-finding sort for houses.

3. **Subdivision profit $** — what you clear after doing the development: gross
   realisation from the lots, less acquisition, less development costs. A
   separate calculation with its own inputs, NOT a form of margin.

4. **Subdivision profit %** — (3) as a return on total cost. Needed because the
   dollar figure alone ranks a $500k gain on a $5M site above a $400k gain on a
   $1M site, when the second is by far the better deal. Sorting on $ finds the
   biggest jobs; sorting on % finds the best returns.

Number 3 is why a 5,665 m2 Saint Heliers site flagged +12 lots carried a buy
ceiling of $4.27M against a $3.0M list. The profit was in the lots, not in the
house being underpriced — margin on that listing was near zero.

An agent hunting underpriced houses sorts on (2). An agent hunting development
sites sorts on (3) or (4). Different deals, different buyers, and one sort
cannot serve them.

Default sort: margin descending.

*Sort by "vs CV %" descending* — this is the data-quality sort, not a deal sort.
It turns finding bad valuations from a click-through hunt into a glance. The
+216%, +433% and +547% cases all surface at the top of one screen.

**Add "Download CSV"** exporting the current filtered set, so a batch can be
checked in Excel before going live.

---

## Verify while testing

Property 23851 — CV $610,000, was valued $3,945,000 (+546.7%). With
`ANCHOR_TOLERANCE = 0.40` in `pricing/pipeline.py` this should now come back at
roughly $579,500. If it still shows the old figure, the anchor guard is not
firing and that is a separate bug to trace: find what sets or overwrites
`ollie_value` after the guard at ~line 409, and confirm stored values are being
recomputed rather than served from the previous batch.
