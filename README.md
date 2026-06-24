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
  synthetic data │                    │   multi-subsidiary payroll records
                 └─────────┬──────────┘   + banking products
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

## Quickstart

This project's `env/` folder is a conda environment (Python 3.14) with
`fastapi`, `uvicorn`, `duckdb`, `splink`, `faker`, and `pandas` already
installed. From the project root:

```bash
# Windows
.\env\python.exe -m uvicorn app.main:app --reload --port 8000

# macOS/Linux, or any other Python 3.11+ environment:
pip install -r app/requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open **http://localhost:8000/** for the dashboard, or
**http://localhost:8000/docs** for the interactive Swagger UI.

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
FastAPI at `/`. It has three views, all backed by live calls to the JSON API
below - nothing is hardcoded:

* **Dashboard** - KPI cards (population, records, clusters, duplicates found,
  match confidence, net wealth), an **"Entity Resolution In Action"** before/after
  panel (`GET /dashboard/showcase`) showing one real resolved cluster from the
  current run exactly as it looked pre-Splink (several records that look like
  different people) and post-Splink (one consolidated profile, with a link
  straight into its full Profile page), plus a wealth-by-asset-class donut and
  a records-by-subsidiary bar chart. "Generate Data" and "Run Linkage" buttons
  trigger the corresponding API calls and refresh the view.
* **Directory** - browses all resolved profiles alphabetically by default
  (`GET /search` with an empty `q`), or search by name (`GET /search?q=`);
  click the eye icon on a result to open its full **Profile** page.
* **Profile** - a single resolved person's full dossier (`GET /wealth/{id}/detail`):
  wealth-percentile score and tier, asset breakdown, every linked subsidiary
  source record with its own confidence score, a salary-by-subsidiary chart,
  and a field-by-field "Match Explanation" that calls out exactly which
  identity fields agreed across the linked records and which still vary
  (e.g. differing postcodes/emails) - real output of the data, not a canned
  narrative. Includes working "Export Linked Data" (downloads the dossier as
  JSON) and "Copy Master ID" actions.
* **Data Quality** - a match-confidence histogram, a cluster-size
  distribution, and a "manual review queue" of the lowest-confidence
  multi-record clusters (`GET /quality`) - the closest thing this POC has to
  a human-in-the-loop reconciliation workflow.

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

| Method | Path | Description |
|---|---|---|
| `POST` | `/generate-data` | (Re)generate the synthetic dataset. Returns `{"people": 10000, "records": 25000}`. |
| `POST` | `/run-linkage` | Run the Splink pipeline and rebuild wealth profiles. Returns `{"clusters": ..., "duplicates_found": ...}`. |
| `GET` | `/wealth/{master_person_id}` | Golden wealth profile for one resolved person, e.g. `/wealth/MP00001`. |
| `GET` | `/wealth/{master_person_id}/detail` | Full profile dossier: linked source records, wealth percentile/tier, field-agreement explanation. Backs the Profile page. |
| `GET` | `/search?q=` | Search resolved profiles by name; omit/empty `q` to list all profiles alphabetically. |
| `GET` | `/dashboard` | Platform-wide summary metrics (population, clusters, confidence, asset totals). |
| `GET` | `/dashboard/showcase` | One representative resolved profile (raw linked records + golden profile) for the dashboard's before/after panel. |
| `GET` | `/quality` | Match-confidence histogram, cluster-size distribution, and a manual-review queue. |
| `GET` | `/health` | Liveness/readiness probe. |
| `GET` | `/` | The dashboard frontend. |

Full request/response schemas (with field descriptions) are in the
Swagger UI at `/docs` or the ReDoc view at `/redoc`.

## Reproducibility

Every random choice in `data_generator.py` and `splink_service.py` is seeded
(`SEED = 42`), so `POST /generate-data` followed by `POST /run-linkage`
produces identical results on every run and every machine. On this seeded
dataset the pipeline typically resolves:

* **25,000** source records -> **~10,300** clusters (**~14,700** duplicates found)
* **~100%** pairwise precision and **~98%** pairwise recall against the
  generator's ground truth (measurable only in this synthetic demo - see
  `app/README.md` for how the clustering threshold was chosen using this
  metric)

## Project structure

```
SCV/
├── app/
│   ├── main.py             FastAPI app + endpoints + startup bootstrap
│   ├── data_generator.py   Synthetic data generation (Faker, seeded)
│   ├── splink_service.py   Splink configuration, training, linkage
│   ├── wealth_service.py   Wealth aggregation, search, dashboard/quality queries
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
* Banking products are generated against the bank's own clean customer index
  and attached to a resolved cluster via majority vote (see
  `app/README.md`) - in production the bank's own product holders would
  also need linking, not just trusted as ground truth.
* DuckDB is used as a single-writer embedded database, opened per request;
  this is appropriate for a demo/POC but not for high-concurrency production
  workloads.
* The frontend is intentionally framework-free (no bundler/build step) to
  keep the whole project runnable with a single `uvicorn` command.
