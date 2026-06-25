# Single View of Wealth (SVW) Platform

A proof-of-concept for a banking group that wants to consolidate payroll data
from multiple subsidiaries, resolve duplicate employee identities (no
subsidiary shares a common employee ID, and every subsidiary's data has the
usual real-world quality issues), and produce a single consolidated wealth
view per individual to support personalised banking offers.

Entity resolution is performed with **[Splink](https://moj-analytical-services.github.io/splink/)**
(probabilistic record linkage, DuckDB backend). Everything else - the API,
the dashboard frontend, the synthetic data, the storage - is a self-contained
FastAPI + DuckDB app with no external services.

## Architecture

```
                 ┌────────────────────┐
  Faker-seeded   │  data_generator.py │   10,000 people -> 25,000 noisy
  synthetic data │                    │   multi-subsidiary payroll records +
                 └─────────┬──────────┘   noisy multi-subsidiary banking products
                           │  DuckDB (data/svow.duckdb)
                           v
                 ┌────────────────────┐
                 │  splink_service.py │   train -> predict -> cluster
                 │  (Splink, DuckDB)  │   -> master_person_id clusters
                 └─────────┬──────────┘
                           v
                 ┌────────────────────┐
                 │  wealth_service.py │   aggregate payroll + banking
                 │                    │   products per cluster -> golden
                 └─────────┬──────────┘   wealth profile
                           v
                 ┌────────────────────┐
                 │     main.py        │   FastAPI: /generate-data,
                 │   (FastAPI app)    │   /run-linkage, /wealth/{id},
                 └─────────┬──────────┘   /search, /dashboard, /quality
                           v
                 ┌────────────────────┐
                 │  app/static/*      │   Dashboard frontend (HTML/JS,
                 │  (served at /)     │   Chart.js) consuming the JSON API
                 └────────────────────┘
```

See [`app/README.md`](app/README.md) for the per-module breakdown and the
detailed rationale behind every Splink configuration choice.

## Authentication

A minimal JWT login (`app/auth.py`) sits in front of every data endpoint. Two demo accounts
are seeded automatically on first startup:

| Username | Password | Role | Can do |
|---|---|---|---|
| `admin` | `admin123` | `admin` | Everything below, plus regenerate data / re-run linkage |
| `analyst` | `analyst123` | `analyst` | Search, view profiles, dashboard, exports |

These are POC-only demo credentials - see [`app/README.md`](app/README.md#authentication-authpy)
for the role matrix and how to override the JWT signing secret (`SVW_JWT_SECRET`) outside of
local/demo use. `/`, `/health`, `/docs`/`/redoc`, and `POST /auth/login` remain public.

## Quickstart

This project's `env/` folder is a conda environment (Python 3.14) with
`fastapi`, `uvicorn`, `duckdb`, `splink`, `faker`, `pandas`, and the
auth/export dependencies (`pyjwt`, `bcrypt`, `python-multipart`,
`reportlab`) already installed. From the project root:

```bash
# Windows
.\env\python.exe -m uvicorn app.main:app --reload --port 8000

# macOS/Linux, or any other Python 3.11+ environment:
pip install -r app/requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open **http://localhost:8000/** for the dashboard (sign in with one of the demo accounts
above), or **http://localhost:8000/docs** for the interactive Swagger UI (use the "Authorize"
button there to log in once and have it applied to every endpoint you try).

On first startup the app automatically:
1. Generates the synthetic dataset (seeded, reproducible).
2. Runs the Splink linkage pipeline.
3. Builds the wealth profiles.

This takes roughly 20-40 seconds; watch the server logs for progress. The
dataset persists to `data/svow.duckdb`, so subsequent restarts skip
regeneration - delete that file (or use the "Generate Data" / "Run Linkage"
buttons in the dashboard) to start fresh.

## Dashboard frontend

`app/static/index.html` + `app/static/app.js` is a small dependency-free
dashboard (Tailwind CDN + Chart.js CDN, no build step) served directly by
FastAPI at `/`. A login overlay (`POST /auth/login`) gates the whole app -
the bearer token is kept in `localStorage` and attached to every API call,
and an admin-only "Generate Data"/"Run Linkage" pair is hidden for the
`analyst` role. It has three views, all backed by live calls to the JSON API
below - nothing is hardcoded:

* **Dashboard** - KPI cards (population, records, clusters, duplicates found,
  match confidence, net wealth), an **"Entity Resolution In Action"** before/after
  panel (`GET /dashboard/showcase`) showing one real resolved cluster from the
  current run exactly as it looked pre-Splink (several records that look like
  different people) and post-Splink (one consolidated profile, with a link
  straight into its full Profile page), plus a wealth-by-asset-class donut, a
  records-by-subsidiary bar chart, and a banking-products-by-subsidiary bar chart.
  "Generate Data" and "Run Linkage" buttons trigger the corresponding API calls and
  refresh the view.
* **Directory** - browses all resolved profiles alphabetically by default
  (`GET /search` with an empty `q`), or search by name (`GET /search?q=`);
  click the eye icon on a result to open its full **Profile** page, or
  "Export CSV" to download the current listing (`GET /export/directory.csv`).
* **Profile** - a single resolved person's full dossier (`GET /wealth/{id}/detail`):
  wealth-percentile score and tier, asset breakdown, every linked subsidiary
  source record with its own confidence score, a salary-by-subsidiary chart, an
  itemized list of this profile's banking products with the subsidiary each one
  sits at and its own confidence score (banking products are resolved by Splink
  exactly like payroll records, not trusted via a clean index), and a field-by-field
  "Match Explanation" that calls out exactly which
  identity fields agreed across the linked records and which still vary
  (e.g. differing postcodes/emails) - real output of the data, not a canned
  narrative. Includes working "Export Linked Data" (downloads the dossier as
  JSON), "Export Profile Report" (downloads it as a PDF, `GET /wealth/{id}/export/pdf`),
  and "Copy Master ID" actions.
* **Data Quality** - a match-confidence histogram, a cluster-size
  distribution, and a "manual review queue" of the lowest-confidence
  multi-record clusters (`GET /quality`) - the closest thing this POC has to
  a human-in-the-loop reconciliation workflow. "Export CSV" downloads the
  full backlog (`GET /export/review-queue.csv`), not just the on-screen top 10.

## Demo script

`demo.py` walks through the full "before/after Splink" story end-to-end from
the command line - no server required:

```bash
.\env\python.exe demo.py        # Windows, using the bundled conda env
python demo.py                  # any environment with app/requirements.txt installed
```

It picks a person with noisy records across 3+ subsidiaries, prints those
records as they'd look *without* entity resolution (apparently different
people), runs the Splink pipeline, then prints the resolved
`master_person_id`, the linked subsidiaries with confidence scores, and the
final Single View of Wealth.

## API endpoints

Every endpoint below except `/health`, `/`, `/docs`/`/redoc`, and `POST /auth/login` requires
`Authorization: Bearer <token>` from `POST /auth/login` - see [Authentication](#authentication).

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/auth/login` | - | Exchange a username/password for a bearer token. Returns `{"access_token": ..., "token_type": "bearer", "role": ...}`. |
| `GET` | `/auth/me` | any | The authenticated user's username and role. |
| `POST` | `/generate-data` | `admin` | (Re)generate the synthetic dataset. Returns `{"people": 10000, "records": 25000}`. |
| `POST` | `/run-linkage` | `admin` | Run the Splink pipeline and rebuild wealth profiles. Returns `{"clusters": ..., "duplicates_found": ...}`. |
| `GET` | `/wealth/{master_person_id}` | any | Golden wealth profile for one resolved person, e.g. `/wealth/MP00001`. |
| `GET` | `/wealth/{master_person_id}/detail` | any | Full profile dossier: linked source records, wealth percentile/tier, field-agreement explanation. Backs the Profile page. |
| `GET` | `/wealth/{master_person_id}/export/pdf` | any | The same dossier, rendered as a downloadable PDF report. |
| `GET` | `/search?q=` | any | Search resolved profiles by name; omit/empty `q` to list all profiles alphabetically. |
| `GET` | `/export/directory.csv?q=` | any | Every profile matching `q` (or all profiles) as CSV - no `/search`-style result cap. |
| `GET` | `/dashboard` | any | Platform-wide summary metrics (population, clusters, confidence, asset totals). |
| `GET` | `/dashboard/showcase` | any | One representative resolved profile (raw linked records + golden profile) for the dashboard's before/after panel. |
| `GET` | `/quality` | any | Match-confidence histogram, cluster-size distribution, and a manual-review queue. |
| `GET` | `/export/review-queue.csv?limit=` | any | The manual-review backlog as CSV (default 500, not just the dashboard's top 10). |
| `GET` | `/health` | - | Liveness/readiness probe. |
| `GET` | `/` | - | The dashboard frontend. |

Full request/response schemas (with field descriptions) are in the
Swagger UI at `/docs` or the ReDoc view at `/redoc`.

## Reproducibility

Every random choice in `data_generator.py` and `splink_service.py` is seeded
(`SEED = 42`), so `POST /generate-data` followed by `POST /run-linkage`
produces identical results on every run and every machine.

Splink now resolves payroll *and* banking-product records together in one pool
(rather than trusting banking products via a clean index), so the linkage pool is
meaningfully larger than payroll alone. On this seeded dataset the pipeline
currently resolves:

* **~25,000** payroll records + **~24,000** banking-product records (**~49,000**
  total noisy records) -> **~10,150** clusters (**~38,900** duplicates found)
* **~100%** average per-record match confidence

The **~100% pairwise precision / ~98% pairwise recall** figures quoted in
`app/README.md` were measured by sweeping `CLUSTER_MATCH_THRESHOLD` against the
generator's ground truth back when the pool was payroll-only; that sweep has not
been re-run against the larger, blended pool, since this synthetic demo has no
in-repo tooling for it beyond the one-off dev exercise described there.

## Project structure

```
SCV/
├── app/
│   ├── main.py             FastAPI app + endpoints + startup bootstrap
│   ├── data_generator.py   Synthetic data generation (Faker, seeded)
│   ├── splink_service.py   Splink configuration, training, linkage
│   ├── wealth_service.py   Wealth aggregation, search, dashboard/quality queries
│   ├── auth.py             JWT login, password hashing, role-based access
│   ├── exports.py          CSV/PDF report rendering
│   ├── models.py           Internal domain models
│   ├── schemas.py          API request/response schemas
│   ├── static/             Dashboard frontend (index.html, app.js)
│   ├── requirements.txt
│   └── README.md           Module-level documentation
├── demo.py                 Before/after Splink CLI walkthrough
├── data/                   DuckDB file lives here (gitignored)
└── env/                    Local conda environment
```

## Notes & known simplifications (POC scope)

* All amounts are generated in GBP only; the `currency` field exists for
  schema completeness but no FX conversion is implemented.
* Salaries and account balances are sampled from right-skewed (lognormal)
  distributions rather than `uniform(low, high)` - most salaries/balances sit
  well below the stated maximum with a shrinking population stretching toward
  it, which is what real income and wealth distributions look like (a flat
  uniform spread looks obviously synthetic by comparison). Mortgages scale
  with salary via a triangular distribution peaking at a ~3.5x affordability
  multiple. Net wealth only counts cash + savings + investments - mortgage
  (per the spec's formula) - it does not model property value as an offsetting
  asset, so "payroll + mortgage only" customers necessarily show negative net
  wealth by design, not as a bug.
* Banking products carry the same kind of noisy, independently-captured identity
  fields as payroll records, and are resolved by the *same* Splink pipeline rather than
  trusted via a clean customer index - closing what used to be a known simplification
  here. A person can hold multiple accounts of the same product type across different
  subsidiaries (e.g. two savings accounts, one at Calder Wealth Partners, one at
  Ridgeway Private Bank), each independently noised, reflecting how a banking group's
  customers really do scatter products across its subsidiary banks and how each
  subsidiary's own system would record them imperfectly. One simplification this
  introduces: product records reuse payroll's exact noise model (same nickname/
  abbreviation/typo functions, same missing-value rates) rather than modeling
  product-specific data-quality patterns separately.
* DuckDB is used as a single-writer embedded database, opened per request;
  this is appropriate for a demo/POC but not for high-concurrency production
  workloads.
* The frontend is intentionally framework-free (no bundler/build step) to
  keep the whole project runnable with a single `uvicorn` command.
* Authentication is two seeded demo accounts and a 60-minute JWT with no
  refresh flow - there's no signup, password reset, or user-management UI.
  The token is kept in browser `localStorage`, not an httpOnly cookie, which
  is adequate for a local demo but not a production hardening posture.
