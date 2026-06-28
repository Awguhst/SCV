# `app/` package reference

This package implements the Single View of Wealth (SVW) proof-of-concept.
For setup/run instructions, see the [top-level README](../README.md).
This file documents what each module is responsible for and the key
design decisions inside it.

## Module map

| Module | Responsibility |
|---|---|
| `models.py` | Internal domain models (Pydantic) describing each DuckDB table's row shape: `Person` (ground truth), `Record` (a noisy per-subsidiary record - payroll or banking-product, distinguished by `record_type`), `ClusterAssignment`, `WealthProfile`. |
| `schemas.py` | Public API request/response contracts (Pydantic), kept separate from `models.py` so the OpenAPI schema can evolve independently of the storage layer. |
| `data_generator.py` | Seeded synthetic data generation: 10,000 ground-truth people, 25,000 noisy multi-subsidiary payroll records, and a variable number of noisy multi-subsidiary banking-product records (same name/address/email-variant, missing-value noise model as payroll), all persisted into one unified `records` table. Every distribution parameter (population scale, record/bundle/account-count distributions, salary/bonus/balance/mortgage distributions, identity-noise thresholds) is a named module-level constant with a sensible default. |
| `splink_service.py` | Splink configuration and the train -> predict -> cluster pipeline, run once over every row of the unified `records` table (payroll and banking-product alike). Persists `clusters` (source_record_id, master_person_id, match_probability). |
| `wealth_service.py` | Aggregates clusters + banking products into `wealth_profiles`; exposes lookup, search, dashboard summary, and data-quality queries. |
| `auth.py` | JWT-based login: the `users` table (seeded with two demo accounts), password hashing, token issuing/verification, and the `get_current_user`/`require_role` FastAPI dependencies. |
| `exports.py` | Server-rendered exports: CSV for the directory listing and the manual-review queue, PDF for a single profile dossier (via `reportlab`). Pure rendering on top of `wealth_service`'s existing queries - no new DB access. |
| `main.py` | FastAPI app wiring, lifespan auto-bootstrap, static-frontend mount, and the HTTP endpoints. |
| `static/` | The dashboard frontend (`index.html` + `app.js`) - plain HTML/JS + Tailwind/Chart.js CDN, served at `/`. Talks to the JSON API only; no server-side templating. |

## Authentication (`auth.py`)

A minimal JWT login sits in front of every data endpoint. Two roles:

| Role | Can do |
|---|---|
| `admin` | Everything an analyst can, plus `POST /generate-data` and `POST /run-linkage`. |
| `analyst` | Read/search/export the resolved wealth profiles. |

Two demo accounts are seeded into a `users` table on first startup (`admin`/`admin123`,
`analyst`/`analyst123` - see `DEMO_USERS` in `auth.py`) - POC-only credentials, not meant for
production use. `/`, `/health`, `/docs`, `/redoc`, `/static/*`, and `POST /auth/login` stay
public; everything else requires `Authorization: Bearer <token>` from `POST /auth/login`.
The JWT signing secret comes from `SVW_JWT_SECRET` - falls back to an insecure dev default
(logged as a warning) if unset, so the app still runs out of the box for local/demo use.

## Data model (DuckDB tables)

```
persons              -- ground truth only: person_index, name, dob, email, phone, address, city, postcode
records              -- ~49,000 noisy rows, one unified table for every kind of subsidiary record:
                         source_record_id, person_index (hidden FK), subsidiary, record_type
                         (PAYROLL/CURRENT_ACCOUNT/SAVINGS_ACCOUNT/INVESTMENT/MORTGAGE), name/contact
                         fields (with noise/nulls, same model for every record_type), plus whichever
                         payload columns apply to that record_type - employee_id/annual_salary/bonus
                         for PAYROLL, account_id/balance for the four product types (all nullable,
                         populated only for the relevant record_type), currency
clusters             -- source_record_id, master_person_id, match_probability   (Splink output, covering
                         every row of `records`, regardless of record_type)
wealth_profiles       -- master_person_id, name, salary, cash, savings, investments, mortgage, net_wealth
users                 -- username, password_hash, role, created_at  (login accounts - see Authentication below)
```

A person may hold zero, one, or several rows of a given product type, each tagged
with the subsidiary it sits at and independently noised - reflecting how a real
banking group's customers scatter products across its subsidiary banks (a mortgage
at one, savings at another), each captured imperfectly by that subsidiary's own
system, not just a single undifferentiated holding per type.

`persons` and the `person_index` column on `records` exist only because this is a
*synthetic* demo - they represent ground truth the data generator knows but a real bank
never would. They're what let `demo.py` show a true "before/after" comparison and let the
threshold sweep in development be checked against real precision/recall. A production system
would not have a `persons` table; it would only ever see `records`.

## Why Splink, and why these specific settings

See the module docstring and inline comments in `splink_service.py` for the full rationale.
In short:

* **`dedupe_only`** link type because every noisy record - payroll *and* banking-product
  alike, from all subsidiaries - lives in one pool to be deduplicated, not two datasets
  to be linked. `splink_service._load_linkage_pool` is what reads every row of the unified
  `records` table before Splink ever sees it.
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
  possible in this synthetic demo), back when the linkage pool was payroll-only. 0.5-0.9 all
  produced identical, near-optimal results (precision ~100%, recall ~98%); 0.75 sits in the
  middle of that stable plateau. Now that banking-product records share the same pool, this
  threshold has not been re-swept against the larger, blended dataset - the noise model is the
  same, but the absolute precision/recall figures above should be treated as historical
  (payroll-only) until re-measured.

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
* Banking products are no longer a clean, ground-truth attachment. Each banking-product row
  in the unified `records` table (any `record_type` other than `PAYROLL`) carries the same
  kind of noisy, independently-captured identity fields as a payroll record, flows through
  the *same* Splink dedupe pool, and attaches to a `master_person_id` exactly the way a
  payroll record does - by being a member of that cluster in the `clusters` table. A person
  may hold several accounts of the same type across different subsidiaries (e.g. savings at
  one subsidiary, a mortgage at another), and - since attachment is now genuinely resolved
  rather than trusted - a product's linkage is subject to the same kind of error a noisy
  payroll record always has been (see `wealth_service.py`'s `name_counts` CTE, which pulls
  names from every row of `records` regardless of `record_type`, so a cluster never ends up
  nameless even in the edge case where every payroll record for a person splits away from
  their product records).

## Profile dossier endpoint (`GET /wealth/{id}/detail`)

Backs the dashboard's "Profile" page (reached by clicking a Directory search result).
Everything on it is derived, not hardcoded:

* `wealth_score` - this profile's `net_wealth` percentile rank (0-100) among all resolved
  profiles (`PERCENT_RANK() OVER (ORDER BY net_wealth)`), and `wealth_tier` is a simple
  threshold-based label on top of it (Mass Market / Affluent / High Net Worth / Ultra High
  Net Worth / Negative Equity) - a simplified version of real banking proposition segments.
* `records` - every row of the unified `records` table clustered into this profile - payroll
  and banking-product alike, distinguished by `record_type` - each with its own per-record
  `match_probability`. This is the real "data lineage": which subsidiary, which
  `record_type`, what that subsidiary's system recorded (`employee_id`/`annual_salary`/
  `bonus` for payroll rows, `account_id`/`balance` for banking-product rows - nullable,
  populated only for the relevant `record_type`). The frontend's "Data Lineage" panel
  filters this list to `record_type === "PAYROLL"`; the "Financial Holdings" panel shows
  every record. A profile backed entirely by banking products (no linked payroll record at
  all) is possible - a rare linkage-split edge case - in which case the filtered payroll
  view is simply empty.
* `field_agreement` - for each identity field (email, phone, address, postcode,
  date_of_birth, last_name), whether *every* linked record - payroll and banking-product
  alike - agreed on a single non-null value. Where they don't, the distinct values are
  surfaced (e.g. "TF2R 2LQ vs. TF2R2LQ") so an analyst can see exactly what was noisy versus
  what was a stronger signal - this is what the page's "Match Explanation" panel renders,
  instead of a fabricated narrative.

## Data-quality endpoint (`GET /quality`)

Backs the dashboard's "Data Quality" page. All three pieces are computed directly from the
`clusters` table (no extra ground truth needed, so this works the same way in production):

* `match_probability_histogram` - bucketed counts of the per-record confidence score described
  above, so an analyst can see at a glance how much of the population is high- vs low-confidence.
* `cluster_size_distribution` - how many resolved people had 1, 2, 3, or 4+ linked records,
  counting every record in the cluster (payroll *and* banking-product). The "4+" bucket no
  longer implies a likely false-merge linkage error the way it did when the pool was
  payroll-only (capped at 4 records per person by construction) - someone with several
  banking products can legitimately have a large, correctly-resolved cluster now.
* `review_queue` - the lowest-confidence *multi-record* clusters (singletons are excluded - there's
  nothing to review), worst first. This is the closest thing this POC has to a human-in-the-loop
  reconciliation queue: in production, a bank wouldn't blindly trust every Splink merge, it would
  route the borderline ones to an analyst.

## Exports (`exports.py`)

Three server-rendered downloads, all requiring an authenticated user (any role):

* `GET /export/directory.csv` - every resolved profile matching `?q=` (or all profiles, if
  blank) as CSV. Calls `wealth_service.search_person` with a much larger limit than its
  on-screen default of 50, so the export never silently truncates the dataset it claims to cover.
* `GET /export/review-queue.csv` - the manual-review backlog as CSV, defaulting to the worst
  500 clusters rather than the dashboard widget's top 10 (`?limit=` to override).
* `GET /wealth/{id}/export/pdf` - one profile's full dossier (same data as `/wealth/{id}/detail`)
  rendered as a PDF report via `reportlab`, alongside the dashboard's existing client-side-only
  JSON export.

## Dashboard showcase endpoint (`GET /dashboard/showcase`)

Backs the dashboard's "Entity Resolution In Action" before/after panel. `wealth_service.get_showcase_example`
picks one cluster straight out of the live `clusters`/`records` tables - no ground truth involved,
so the same query would work against real production data:

* Selection criteria: 3-4 linked records (the generator never gives one ground-truth person *more*
  than 4 - a bigger cluster than that is itself a false-merge linkage error, not a clean example) with
  at least 2 different first-name spellings/variants, so the "before" side visibly looks like
  unrelated people. Among qualifying clusters, picks the largest deterministically.
* Returns the exact same shape as `/wealth/{id}/detail`, so the frontend renders the "before" raw
  records and the "after" golden profile from one response.
