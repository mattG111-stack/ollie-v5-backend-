# Four-stage ingest — build notes

Splits the welded `ingest_for_sale` job into four operator-triggered stages, each
independently re-runnable, with progress that lives in the database rather than
the browser.

```
LOAD  ──▶  ENRICH  ──▶  PRICE  ──▶  PUBLISH
 CSV       CoreLogic     AVM        live
 secs      ~1hr          minutes    instant
```

---

## What changed, and what didn't

The pricing itself is untouched. `PRICE` runs through `reprice.reprice_batch`,
which reconstructs the exact pipeline input from each stored row and writes back
only the pipeline output columns — the same code path the welded ingest ran.
`reprice.validate_noop()` is the existing trust gate for this: re-price an
unchanged batch and confirm it reproduces the stored `fair_value`. Splitting the
stages does not move a single number.

`release.py` and `routers/release.py` are untouched — `PUBLISH` calls
`publish_release` as before.

`ingest_for_sale` is retained for the one-shot path and for `scripts/`. Nothing
that called it breaks.

### The one substantive change: when rejects fire

The old ingest applied every reject rule at write time. That was only correct
because CoreLogic had already run inside the same function. Once the stages come
apart it stops holding — **a row with no floor area is exactly the row enrich
exists to fix**, so rejecting it at load would delete the evidence before the
stage that repairs it ever runs.

So the rules are now split:

| Applied at LOAD (hard — no lookup can flip these) | Applied at PRICE (soft — enrich may supply the field) |
|---|---|
| `non_target_region` | `no_cv` |
| `no_suburb` | `asking_vs_cv_50pct` |
| `placeholder_asking` (<$10k) | `dwelling_no_floor` |
| `empty_row` | |

Soft rules are applied as **holds, not deletions**, so a rejected row stays
visible in the grid instead of vanishing between stages.

---

## Answering "did CoreLogic actually finish?"

This is the specific failure — job #3 hit 30%, the browser dropped, and there was
no way to tell running from stalled from dead. Four things now cover it:

**1. Counters written to the DB as the stage runs.** `rows_processed / rows_total`,
plus `rows_filled`, `rows_missed`, `rows_skipped` — flushed every 10 rows, which
at 0.5s per lookup is a five-second write cadence. Refresh the page, close the
laptop, come back tomorrow: the numbers are correct.

**2. `missed` recorded separately from failed.** CoreLogic returns nothing for
plenty of addresses. Those rows are marked `missed` and price on what they
already have. The stage still completes.

**3. Terminal states.** `completed` / `failed` / `cancelled`, each with
`started_at` and `completed_at`.

**4. A heartbeat, for the case a terminal state never gets written.** If the
container is replaced mid-run, nothing marks the job failed and it sits at 62%
looking alive forever. `heartbeat_at` is touched on every flush;
`reap_stale_jobs()` runs on every status poll and converts jobs with a heartbeat
older than 10 minutes to `failed`, with a message saying how many rows were saved
and that re-running will resume.

There is also `enrich_coverage(batch_id)`, derived from the **listings
themselves** rather than any job row — so it is still right even if the job row
was lost.

### Resumability

Per-row `enrich_status` (`pending` / `filled` / `missed` / `skipped`) means a run
that dies at 60% leaves the first 60% marked. Re-running queries only `pending`
rows. **A test proves this**: kill enrich after 6 of 10 rows, re-run, assert
exactly 4 further CoreLogic calls are made.

Cancel is co-operative — the worker finishes the row it's on, commits, and writes
`cancelled`. Completed work is kept, so a cancelled enrich resumes rather than
restarts.

---

## API contract for the admin page

```
POST /api/admin/stages/load                  multipart: for_sale/sold/rent → [job]
POST /api/admin/stages/{batch_id}/enrich     → job
POST /api/admin/stages/{batch_id}/price      → job
POST /api/admin/stages/{batch_id}/publish    → job   (409 if any row unpriced)
POST /api/admin/stages/jobs/{job_id}/cancel  → job

GET  /api/admin/stages/batches               → pick a batch
GET  /api/admin/stages/{batch_id}            → all four stage states  ← poll this
GET  /api/admin/stages/{batch_id}/rows       → the grid
GET  /api/admin/stages/jobs/recent           → history
```

`GET /api/admin/stages/{batch_id}` is the single poll the page needs. Each of the
four stages returns `status`, `progress_pct`, `rows_processed`, `rows_total`,
`rows_filled`, `rows_missed`, `rows_skipped`, `detail`, `error`, timestamps,
`heartbeat_at`, and — the useful part for the buttons — **`can_run` and
`blocked_reason`**. Drive button enablement straight off `can_run`; render
`blocked_reason` as the tooltip. The response also carries `server_time` so the
page can show "last seen 4 minutes ago" without trusting the client clock.

`GET .../rows` supports `?enrich_status=`, `?held_only=true`, `?unpriced_only=true`,
`limit`, `offset`. Valuation columns are null until PRICE has run — that's the
point: you inspect what arrived before spending an hour of CoreLogic calls on it.

Concurrency is guarded server-side: a second stage on the same batch gets a 409.

---

## Files

| File | |
|---|---|
| `alembic/versions/a9f4c2e81b30_stage_pipeline.py` | new | migration |
| `app/stages.py` | new | the four runners, `JobCtx`, reaper, state |
| `app/routers/admin_stages.py` | new | endpoints |
| `tests/test_stages.py` | new | 10 tests |
| `app/models.py` | edit | `IngestJob` + 8 cols, `PropertyForSale` + 4 cols |
| `app/ingest.py` | edit | `load_for_sale()`, reject-rule split |
| `app/main.py` | edit | register router |

Migration chains onto `f2a3b4c5d6e7`; single head, verified.

Existing rows are backfilled to `enrich_status='skipped'` — they went through the
old welded pipeline, so they're already enriched and priced, and marking them
`pending` would make a future enrich re-pay for lookups already made.

---

## Not done

**The admin UI.** That's the frontend repo — a batch picker, the grid, and four
buttons wired to the endpoints above. The API is shaped to make it a thin page:
one poll, `can_run` drives the buttons, `detail` is the status line.

**Ordering guarantee across stages.** Nothing stops you running PRICE before
ENRICH — it will price on unenriched attributes, which is a legitimate thing to
want. If you'd rather it be refused, that's a condition in `batch_stage_states`.

**Multi-worker safety.** The concurrency guard is a DB check, not a lock. Fine on
one Railway instance; if you ever scale the backend past one, this needs a real
advisory lock.
