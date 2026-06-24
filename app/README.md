# `app/` package reference

This package implements the Single View of Wealth (SVW) proof-of-concept.
For setup/run instructions, see the [top-level README](../README.md).
This file documents what each module is responsible for and the key
design decisions inside it.

## Module map

| Module | Responsibility |
|---|---|
| `models.py` | Internal domain models (Pydantic) describing each DuckDB table's row shape: `Person` (ground truth), `SourceRecord` (noisy per-subsidiary record), banking-product rows, `ClusterAssignment`, `WealthProfile`. |
| `schemas.py` | Public API request/response contracts (Pydantic), kept separate from `models.py` so the OpenAPI schema can evolve independently of the storage layer. |
| `data_generator.py` | Seeded synthetic data generation: 10,000 ground-truth people, 25,000 noisy multi-subsidiary payroll records (name/address/email variants, missing values), and banking products. Persists everything to DuckDB. |
| `splink_service.py` | Splink configuration and the train -> predict -> cluster pipeline. Persists `clusters` (source_record_id, master_person_id, match_probability) and `person_cluster_map`. |
| `wealth_service.py` | Aggregates clusters + banking products into `wealth_profiles`; exposes lookup, search, dashboard summary, and data-quality queries. |
| `main.py` | FastAPI app wiring, lifespan auto-bootstrap, static-frontend mount, and the HTTP endpoints. |
| `static/` | The dashboard frontend (`index.html` + `app.js`) - plain HTML/JS + Tailwind/Chart.js CDN, served at `/`. Talks to the JSON API only; no server-side templating. |

## Data model (DuckDB tables)

```
persons              -- ground truth only: person_index, name, dob, email, phone, address, city, postcode
source_records       -- 25,000 noisy rows: source_record_id, person_index (hidden FK), subsidiary,
                         employee_id, name/contact fields (with noise/nulls), annual_salary, bonus, currency
current_accounts     -- account_id, person_index, account_balance
savings_accounts     -- account_id, person_index, savings_balance
investments          -- account_id, person_index, investment_balance
mortgages            -- account_id, person_index, mortgage_balance
clusters             -- source_record_id, master_person_id, match_probability   (Splink output)
person_cluster_map   -- person_index -> master_person_id (majority vote; used to attach banking products)
wealth_profiles       -- master_person_id, name, salary, cash, savings, investments, mortgage, net_wealth
```

`persons` and the `person_index` column on `source_records` exist only because this is a
*synthetic* demo - they represent ground truth the data generator knows but a real bank
never would. They're what let `demo.py` show a true "before/after" comparison and let the
threshold sweep in development be checked against real precision/recall. A production system
would not have a `persons` table; it would only ever see `source_records`.

## Why Splink, and why these specific settings

See the module docstring and inline comments in `splink_service.py` for the full rationale.
In short:

* **`dedupe_only`** link type because all subsidiaries' records live in one pool to be
  deduplicated, not two datasets to be linked.
* **`DuckDBAPI` backend** keeps the whole pipeline in-process and dependency-free.
* **Comparisons** use Splink's purpose-built templates (`NameComparison`, `DateOfBirthComparison`,
  `EmailComparison`, `PostcodeComparison`) for the fields where naive exact-match would fail on the
  injected noise, and Levenshtein-distance comparisons for free-text phone/address fields.
* **Blocking rules** are a union of six different "this pair is plausibly the same person" signals
  (shared email, shared phone, name+surname, DOB+postcode, surname+postcode, initial+surname+DOB) -
  this keeps the candidate-pair count tractable while still catching every noise pattern the
  generator injects.
* **Training** uses a small set of high-precision deterministic rules to seed the prior, then
  `estimate_u_using_random_sampling` plus three EM passes against different blocking rules
  (Splink's standard identifiability pattern - each comparison must be trained on data it wasn't
  blocked on).
* **Cluster threshold** (`CLUSTER_MATCH_THRESHOLD = 0.75`) was chosen by sweeping thresholds from
  0.3 to 0.95 and measuring pairwise precision/recall against the generator's ground truth (only
  possible in this synthetic demo). 0.5-0.9 all produced identical, near-optimal results
  (precision ~100%, recall ~98%); 0.75 sits in the middle of that stable plateau.

## Per-record `match_probability`

Splink scores pairwise edges, not individual records, so a single confidence value per
`source_record_id` is derived in `splink_service._per_record_match_probability`: the strongest
edge connecting that record to another member of its own cluster. A record in a singleton
cluster (no duplicate found) defaults to `1.0` - there's no second record to be uncertain against.

## Wealth aggregation choices

* `salary` is **averaged** across a cluster's linked records (duplicates represent the same
  underlying job recorded slightly differently by each subsidiary feed; summing would double-count it).
* `cash` / `savings` / `investments` / `mortgage` are **summed** (a person can hold several
  accounts of the same type, and each contributes real wealth/liability).
* Banking products are generated against the bank's own clean customer index (`person_index`)
  and attached to a `master_person_id` via `person_cluster_map` - representing the bank's
  already-deduplicated core-banking customer base being enriched with newly-linked, noisier
  external payroll data.

## Profile dossier endpoint (`GET /wealth/{id}/detail`)

Backs the dashboard's "Profile" page (reached by clicking a Directory search result).
Everything on it is derived, not hardcoded:

* `wealth_score` - this profile's `net_wealth` percentile rank (0-100) among all resolved
  profiles (`PERCENT_RANK() OVER (ORDER BY net_wealth)`), and `wealth_tier` is a simple
  threshold-based label on top of it (Mass Market / Affluent / High Net Worth / Ultra High
  Net Worth / Negative Equity) - a simplified version of real banking proposition segments.
* `linked_records` - the actual raw `source_records` rows clustered into this profile, each
  with its own per-record `match_probability`. This is the real "data lineage" - which
  subsidiary, which employee_id, what that subsidiary's system recorded.
* `field_agreement` - for each identity field (email, phone, address, postcode,
  date_of_birth, last_name), whether every linked record agreed on a single non-null value.
  Where they don't, the distinct values are surfaced (e.g. "TF2R 2LQ vs. TF2R2LQ") so an
  analyst can see exactly what was noisy versus what was a stronger signal - this is what
  the page's "Match Explanation" panel renders, instead of a fabricated narrative.

## Data-quality endpoint (`GET /quality`)

Backs the dashboard's "Data Quality" page. All three pieces are computed directly from the
`clusters` table (no extra ground truth needed, so this works the same way in production):

* `match_probability_histogram` - bucketed counts of the per-record confidence score described
  above, so an analyst can see at a glance how much of the population is high- vs low-confidence.
* `cluster_size_distribution` - how many resolved people had 1, 2, 3, or 4+ linked records.
* `review_queue` - the lowest-confidence *multi-record* clusters (singletons are excluded - there's
  nothing to review), worst first. This is the closest thing this POC has to a human-in-the-loop
  reconciliation queue: in production, a bank wouldn't blindly trust every Splink merge, it would
  route the borderline ones to an analyst.

## Dashboard showcase endpoint (`GET /dashboard/showcase`)

Backs the dashboard's "Entity Resolution In Action" before/after panel. `wealth_service.get_showcase_example`
picks one cluster straight out of the live `clusters`/`source_records` tables - no ground truth involved,
so the same query would work against real production data:

* Selection criteria: 3-4 linked records (the generator never gives one ground-truth person *more*
  than 4 - a bigger cluster than that is itself a false-merge linkage error, not a clean example) with
  at least 2 different first-name spellings/variants, so the "before" side visibly looks like
  unrelated people. Among qualifying clusters, picks the largest deterministically.
* Returns the exact same shape as `/wealth/{id}/detail`, so the frontend renders the "before" raw
  records and the "after" golden profile from one response.
