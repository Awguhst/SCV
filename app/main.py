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

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

from app import auth, data_generator, exports, splink_service, wealth_service
from app.schemas import (
    DashboardSummaryResponse,
    GenerateDataResponse,
    HealthResponse,
    QualityResponse,
    RunLinkageResponse,
    SearchResponse,
    SearchResultItem,
    SegmentationResponse,
    SegmentMembersResponse,
    TokenResponse,
    UserResponse,
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
    auth.seed_demo_users()
    logger.info("Demo users ready (admin, analyst).")

    if not data_generator.has_generated_data():
        logger.info("No data found - generating synthetic dataset (10,000 people / ~25,000 payroll + banking-product records)...")
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


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
def login(form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """Exchange a username/password for a bearer token. Demo accounts:
    `admin` / `admin123` (full access) and `analyst` / `analyst123`
    (read/export access) - see `app/auth.py`."""
    user = auth.authenticate_user(form.username, form.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = auth.create_access_token(user["username"], user["role"])
    return TokenResponse(access_token=token, role=user["role"])


@app.get("/auth/me", response_model=UserResponse, tags=["Auth"])
def me(user: dict = Depends(auth.get_current_user)) -> UserResponse:
    return UserResponse(username=user["username"], role=user["role"])


@app.post(
    "/generate-data",
    response_model=GenerateDataResponse,
    tags=["Data Generation"],
    dependencies=[Depends(auth.require_role("admin"))],
)
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


@app.post(
    "/run-linkage",
    response_model=RunLinkageResponse,
    tags=["Entity Resolution"],
    dependencies=[Depends(auth.require_role("admin"))],
)
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


@app.get(
    "/wealth/{master_person_id}",
    response_model=WealthProfileResponse,
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
def get_wealth(master_person_id: str) -> WealthProfileResponse:
    """Return the aggregated golden wealth profile for a resolved person,
    e.g. `/wealth/MP00001`."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    profile = wealth_service.get_wealth_profile(master_person_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile found for '{master_person_id}'")
    return WealthProfileResponse(**profile)


@app.get(
    "/wealth/{master_person_id}/detail",
    response_model=WealthProfileDetailResponse,
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
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


@app.get(
    "/wealth/{master_person_id}/export/pdf",
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
def export_wealth_pdf(master_person_id: str) -> Response:
    """Download the full profile dossier (same data as `/detail`) as a PDF
    report."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    profile = wealth_service.get_profile_detail(master_person_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile found for '{master_person_id}'")
    pdf_bytes = exports.build_profile_pdf(profile)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{master_person_id}.pdf"'},
    )


@app.get(
    "/search",
    response_model=SearchResponse,
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
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


@app.get(
    "/dashboard",
    response_model=DashboardSummaryResponse,
    tags=["Dashboard"],
    dependencies=[Depends(auth.get_current_user)],
)
def dashboard() -> DashboardSummaryResponse:
    """High-level summary metrics for the SVW platform: population size,
    linkage quality, and aggregate wealth under management."""
    summary = wealth_service.get_dashboard_summary()
    return DashboardSummaryResponse(**summary)


@app.get(
    "/dashboard/showcase",
    response_model=WealthProfileDetailResponse,
    tags=["Dashboard"],
    dependencies=[Depends(auth.get_current_user)],
)
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


@app.get(
    "/quality",
    response_model=QualityResponse,
    tags=["Dashboard"],
    dependencies=[Depends(auth.get_current_user)],
)
def quality() -> QualityResponse:
    """Linkage-quality diagnostics for the Data Quality page: a match-confidence
    histogram, cluster-size distribution, and a manual-review queue of the
    lowest-confidence multi-record clusters."""
    if not splink_service.has_run_linkage():
        raise HTTPException(status_code=400, detail="No linkage results found. Call POST /run-linkage first.")
    return QualityResponse(**wealth_service.get_quality_metrics())


@app.get(
    "/segmentation",
    response_model=SegmentationResponse,
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
def segmentation() -> SegmentationResponse:
    """Groups every resolved wealth profile into the bank's 5 net-worth
    tiers (Negative Equity / Mass Market / Affluent / High Net Worth /
    Ultra High Net Worth) and returns per-tier summary stats - feeds the
    Employee Segments page's summary cards and charts."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    return SegmentationResponse(**wealth_service.get_segmentation_summary())


@app.get(
    "/segmentation/{tier}/members",
    response_model=SegmentMembersResponse,
    tags=["Wealth"],
    dependencies=[Depends(auth.get_current_user)],
)
def segmentation_members(
    tier: str,
    limit: int = Query(50, description="Max members to return, highest net worth first."),
) -> SegmentMembersResponse:
    """Resolved profiles assigned to one net-worth tier, e.g.
    `/segmentation/Affluent/members` - powers the Employee Segments
    drill-down table. `tier` must be one of the 5 known tier labels."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    try:
        rows, total = wealth_service.get_segment_members(tier, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
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
    return SegmentMembersResponse(wealth_tier=tier, results=results, total=total)


@app.get(
    "/export/directory.csv",
    tags=["Exports"],
    dependencies=[Depends(auth.get_current_user)],
)
def export_directory_csv(
    q: str = Query("", description="Same filter as /search?q= - leave empty to export the full directory."),
) -> Response:
    """Download every resolved profile matching `q` (or all profiles) as CSV."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    csv_text = exports.build_directory_csv(q)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="directory.csv"'},
    )


@app.get(
    "/export/review-queue.csv",
    tags=["Exports"],
    dependencies=[Depends(auth.get_current_user)],
)
def export_review_queue_csv(
    limit: int = Query(
        exports.DEFAULT_REVIEW_QUEUE_EXPORT_LIMIT,
        description="Max number of lowest-confidence clusters to include, worst first.",
    ),
) -> Response:
    """Download the manual-review backlog (low-confidence multi-record
    clusters) as CSV - the full backlog, not just the dashboard's top 10."""
    if not splink_service.has_run_linkage():
        raise HTTPException(status_code=400, detail="No linkage results found. Call POST /run-linkage first.")
    csv_text = exports.build_review_queue_csv(limit)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="review-queue.csv"'},
    )


@app.get(
    "/export/segment-members.csv",
    tags=["Exports"],
    dependencies=[Depends(auth.get_current_user)],
)
def export_segment_members_csv(
    tier: str = Query(..., description="Wealth tier to export, e.g. 'Affluent' - same labels shown on the Employee Segments page."),
) -> Response:
    """Download every resolved profile assigned to one net-worth tier as CSV."""
    if not wealth_service.has_wealth_profiles():
        raise HTTPException(status_code=400, detail="No wealth profiles found. Call POST /run-linkage first.")
    try:
        csv_text = exports.build_segment_members_csv(tier)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="segment-{tier.lower().replace(" ", "-")}.csv"'},
    )
