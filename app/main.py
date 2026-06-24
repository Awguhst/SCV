"""FastAPI application for the Single View of Wealth (SVW) platform.

A banking group's subsidiaries each submit their own payroll feeds with
no shared employee identifier and the usual data-quality issues (name
variants, address abbreviations, missing fields). This service:

1. Generates that synthetic multi-subsidiary payroll dataset.
2. Runs a Splink probabilistic record-linkage pipeline to resolve
   duplicate identities into `master_person_id` clusters.
3. Aggregates each cluster's payroll + banking-product holdings into a
   single "golden" wealth profile.
4. Exposes all of the above over a documented REST API (see /docs).

The dataset is generated automatically on first startup (seeded, so it
is reproducible) - no manual setup is required before exploring the API.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import data_generator, splink_service, wealth_service
from app.schemas import (
    DashboardSummaryResponse,
    GenerateDataResponse,
    HealthResponse,
    QualityResponse,
    RunLinkageResponse,
    SearchResponse,
    SearchResultItem,
    WealthProfileDetailResponse,
    WealthProfileResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("svw")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _bootstrap_demo_data() -> None:
    """Generate data and run linkage automatically if this is a fresh
    database, so /docs is immediately explorable without manual setup.
    Safe to skip - existing data/linkage/profiles are left untouched."""
    if not data_generator.has_generated_data():
        logger.info("No data found - generating synthetic dataset (10,000 people / 25,000 records)...")
        result = data_generator.generate_all()
        logger.info("Generated %s people / %s records.", result.people, result.records)
    else:
        logger.info("Existing dataset found - skipping generation.")

    if not splink_service.has_run_linkage():
        logger.info("Running Splink entity-resolution pipeline (this can take ~1 minute)...")
        linkage = splink_service.run_full_pipeline()
        logger.info(
            "Linkage complete: %s clusters, %s duplicates found, avg confidence %.3f (%.1fs).",
            linkage.clusters,
            linkage.duplicates_found,
            linkage.avg_match_probability,
            linkage.training_seconds,
        )
    else:
        logger.info("Existing clusters found - skipping linkage.")

    if not wealth_service.has_wealth_profiles():
        logger.info("Building wealth profiles...")
        n_profiles = wealth_service.build_wealth_profiles()
        logger.info("Built %s wealth profiles.", n_profiles)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_demo_data()
    yield


app = FastAPI(
    title="Single View of Wealth (SVW) Platform",
    description=(
        "Entity-resolution proof-of-concept for a banking group: links noisy "
        "multi-subsidiary payroll records with Splink and aggregates the "
        "result into a single wealth view per individual."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the dashboard frontend. The interactive API docs remain at /docs."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", response_model=HealthResponse, tags=["Health"])
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        data_generated=data_generator.has_generated_data(),
        linkage_run=splink_service.has_run_linkage(),
    )


@app.post("/generate-data", response_model=GenerateDataResponse, tags=["Data Generation"])
def generate_data() -> GenerateDataResponse:
    """Generate (or regenerate) the synthetic dataset: 10,000 ground-truth
    people, 25,000 noisy multi-subsidiary payroll records, and banking
    products. Uses a fixed seed, so repeated calls are reproducible.

    Regenerating drops any existing clusters/wealth profiles, since they
    were computed against the previous dataset - call /run-linkage again
    afterwards.
    """
    result = data_generator.generate_all()
    return GenerateDataResponse(people=result.people, records=result.records)


@app.post("/run-linkage", response_model=RunLinkageResponse, tags=["Entity Resolution"])
def run_linkage() -> RunLinkageResponse:
    """Run the Splink entity-resolution pipeline over the generated source
    records: trains the probabilistic model, predicts pairwise match
    probabilities, clusters records into `master_person_id` groups, and
    rebuilds wealth profiles on top of the new clusters.
    """
    if not data_generator.has_generated_data():
        raise HTTPException(status_code=400, detail="No data found. Call POST /generate-data first.")

    result = splink_service.run_full_pipeline()
    wealth_service.build_wealth_profiles()
    logger.info(
        "Linkage complete: %s clusters, %s duplicates found, avg confidence %.3f (%.1fs).",
        result.clusters,
        result.duplicates_found,
        result.avg_match_probability,
        result.training_seconds,
    )
    return RunLinkageResponse(clusters=result.clusters, duplicates_found=result.duplicates_found)


@app.get("/wealth/{master_person_id}", response_model=WealthProfileResponse, tags=["Wealth"])
def get_wealth(master_person_id: str) -> WealthProfileResponse:
    """Return the aggregated golden wealth profile for a resolved person,
    e.g. `/wealth/MP00001`."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    profile = wealth_service.get_wealth_profile(master_person_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile found for '{master_person_id}'")
    return WealthProfileResponse(**profile)


@app.get("/wealth/{master_person_id}/detail", response_model=WealthProfileDetailResponse, tags=["Wealth"])
def get_wealth_detail(master_person_id: str) -> WealthProfileDetailResponse:
    """Return the full profile dossier for a resolved person: the golden
    wealth profile, every linked subsidiary source record, a relative
    wealth-percentile score/tier, and a field-by-field explanation of how
    confidently Splink agreed those records describe the same individual."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    profile = wealth_service.get_profile_detail(master_person_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile found for '{master_person_id}'")
    return WealthProfileDetailResponse(**profile)


@app.get("/search", response_model=SearchResponse, tags=["Wealth"])
def search(
    q: str = Query(
        "", description="Name to search for, e.g. 'john smith'. Leave empty to browse all profiles alphabetically."
    ),
) -> SearchResponse:
    """Search resolved profiles by name, matching both the chosen display
    name and any underlying linked source record (so a search still finds
    a cluster even if a different name variant was chosen for display).
    An empty query returns all profiles sorted alphabetically by name."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")

    rows, total = wealth_service.search_person(q)
    results = [
        SearchResultItem(
            master_person_id=row["master_person_id"],
            name=row["name"],
            salary=row["salary"],
            cash=row["cash"],
            savings=row["savings"],
            investments=row["investments"],
            mortgage=row["mortgage"],
            net_wealth=row["net_wealth"],
            linked_subsidiaries=list(row["subsidiaries"]),
            record_count=row["record_count"],
            match_probability=round(float(row["match_probability"]), 6),
        )
        for row in rows
    ]
    return SearchResponse(query=q, results=results, total=total)


@app.get("/dashboard", response_model=DashboardSummaryResponse, tags=["Dashboard"])
def dashboard() -> DashboardSummaryResponse:
    """High-level summary metrics for the SVW platform: population size,
    linkage quality, and aggregate wealth under management."""
    summary = wealth_service.get_dashboard_summary()
    return DashboardSummaryResponse(**summary)


@app.get("/dashboard/showcase", response_model=WealthProfileDetailResponse, tags=["Dashboard"])
def dashboard_showcase() -> WealthProfileDetailResponse:
    """One representative resolved profile, picked because its linked
    records show clearly different name spellings - feeds the dashboard's
    "Before / After Splink" panel. Same shape as /wealth/{id}/detail."""
    if not splink_service.has_run_linkage():
        raise HTTPException(status_code=400, detail="No linkage results found. Call POST /run-linkage first.")
    example = wealth_service.get_showcase_example()
    if example is None:
        raise HTTPException(status_code=404, detail="No clusters available to showcase.")
    return WealthProfileDetailResponse(**example)


@app.get("/quality", response_model=QualityResponse, tags=["Dashboard"])
def quality() -> QualityResponse:
    """Linkage-quality diagnostics for the Data Quality page: a match-confidence
    histogram, cluster-size distribution, and a manual-review queue of the
    lowest-confidence multi-record clusters."""
    if not splink_service.has_run_linkage():
        raise HTTPException(status_code=400, detail="No linkage results found. Call POST /run-linkage first.")
    return QualityResponse(**wealth_service.get_quality_metrics())
