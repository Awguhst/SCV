"""API request/response schemas (the FastAPI/OpenAPI contract layer).

Kept separate from `models.py` so the internal domain shape (DuckDB
tables) can evolve independently of what the API promises callers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GenerateDataResponse(BaseModel):
    """Response for POST /generate-data."""

    people: int = Field(..., description="Number of unique ground-truth people generated.")
    records: int = Field(..., description="Total noisy source records generated across all subsidiaries.")


class RunLinkageResponse(BaseModel):
    """Response for POST /run-linkage."""

    clusters: int = Field(..., description="Number of resolved master_person_id clusters.")
    duplicates_found: int = Field(
        ...,
        description="Records (payroll + banking-product) identified as duplicates of another "
        "record (records - clusters).",
    )


class WealthProfileResponse(BaseModel):
    """Response for GET /wealth/{master_person_id}."""

    master_person_id: str
    name: str
    salary: float
    cash: float
    savings: float
    investments: float
    mortgage: float
    net_wealth: float


class SearchResultItem(WealthProfileResponse):
    """A single match returned by GET /search, enriched with linkage provenance."""

    linked_subsidiaries: list[str] = Field(
        ..., description="Subsidiaries whose records were clustered into this person."
    )
    record_count: int = Field(..., description="Number of source records linked into this cluster.")
    match_probability: float = Field(..., description="Average linkage confidence for this cluster.")


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
    total: int = Field(
        ..., description="Total profiles matching the query (or all profiles, if query is empty) - may exceed len(results)."
    )


class DashboardSummaryResponse(BaseModel):
    """Response for GET /dashboard."""

    unique_people: int = Field(..., description="Ground-truth people generated (ALWAYS the data-gen figure).")
    source_records: int = Field(..., description="Total noisy source records ingested from subsidiaries.")
    clusters: int = Field(..., description="Resolved master_person_id clusters after Splink linkage.")
    avg_match_probability: float = Field(..., description="Mean per-record linkage confidence across all clusters.")
    total_assets: float = Field(..., description="Sum of cash + savings + investments across all wealth profiles.")
    # Additional breakdown fields used by the dashboard UI; additive on top of
    # the fields above, which are the literal contract from the brief.
    duplicates_found: int = Field(
        ..., description="Total linked records (payroll + banking-product) minus resolved clusters."
    )
    total_cash: float = Field(..., description="Sum of current-account balances across all wealth profiles.")
    total_savings: float = Field(..., description="Sum of savings balances across all wealth profiles.")
    total_investments: float = Field(..., description="Sum of investment balances across all wealth profiles.")
    total_mortgage: float = Field(..., description="Sum of mortgage balances across all wealth profiles.")
    total_net_wealth: float = Field(..., description="Sum of net_wealth across all wealth profiles.")
    subsidiary_record_counts: dict[str, int] = Field(
        ..., description="Number of source records contributed by each subsidiary."
    )
    product_subsidiary_counts: dict[str, int] = Field(
        ...,
        description="Number of banking-product accounts (current/savings/investment/mortgage, "
        "combined) held at each subsidiary.",
    )


class LinkedRecord(BaseModel):
    """One subsidiary's raw source record, as linked into a resolved profile."""

    source_record_id: str
    subsidiary: str
    employee_id: str
    first_name: str
    last_name: str
    date_of_birth: str
    email: str | None
    phone: str | None
    address: str | None
    city: str | None
    postcode: str | None
    annual_salary: float
    bonus: float
    currency: str
    match_probability: float = Field(..., description="This record's own per-record linkage confidence.")


class ProductHolding(BaseModel):
    """One banking-product account held by a resolved profile, tagged with
    the subsidiary it sits at. Like a payroll source record, this is a
    genuinely Splink-resolved cluster member - match_probability is this
    holding's own per-record linkage confidence, not a trusted ground-truth
    attachment. A person may hold several of these per product type,
    spread across different subsidiaries. Carries the same noisy identity
    fields a payroll LinkedRecord does, since it was captured (and noised)
    the same way by that subsidiary's own system."""

    product_type: str = Field(
        ..., description="One of 'current_account', 'savings_account', 'investment', 'mortgage'."
    )
    account_id: str
    subsidiary: str
    balance: float
    match_probability: float = Field(..., description="This holding's own per-record linkage confidence.")
    first_name: str
    last_name: str
    date_of_birth: str
    email: str | None
    phone: str | None
    address: str | None
    city: str | None
    postcode: str | None


class FieldAgreement(BaseModel):
    """Whether a given identity field agreed across all of a cluster's linked
    records, or varied (and if so, the distinct values seen)."""

    field: str
    is_consistent: bool
    distinct_values: list[str]


class WealthProfileDetailResponse(WealthProfileResponse):
    """Response for GET /wealth/{master_person_id}/detail - the full profile
    dossier page: everything in WealthProfileResponse plus linkage provenance,
    a relative wealth ranking, and field-level match explanation."""

    wealth_tier: str = Field(..., description="Segment derived from net_wealth, e.g. 'High Net Worth'.")
    wealth_score: float = Field(..., description="Percentile rank (0-100) of net_wealth across all resolved profiles.")
    match_probability: float = Field(..., description="Average per-record linkage confidence for this cluster.")
    record_count: int
    linked_subsidiaries: list[str]
    primary_city: str | None
    primary_postcode: str | None
    linked_records: list[LinkedRecord]
    field_agreement: list[FieldAgreement]
    product_holdings: list[ProductHolding] = Field(
        ..., description="Every banking-product account held by this profile, tagged with its subsidiary."
    )


class QualityHistogramBucket(BaseModel):
    label: str
    count: int


class ReviewQueueItem(BaseModel):
    """A low-confidence cluster surfaced for manual analyst review."""

    master_person_id: str
    name: str
    match_probability: float
    record_count: int
    linked_subsidiaries: list[str]


class QualityResponse(BaseModel):
    """Response for GET /quality - feeds the Data Quality dashboard page."""

    match_probability_histogram: list[QualityHistogramBucket]
    cluster_size_distribution: list[QualityHistogramBucket]
    review_queue: list[ReviewQueueItem] = Field(
        ..., description="Lowest-confidence clusters, worst first - candidates for manual analyst review."
    )


class HealthResponse(BaseModel):
    status: str
    data_generated: bool
    linkage_run: bool


class TokenResponse(BaseModel):
    """Response for POST /auth/login."""

    access_token: str
    token_type: str = "bearer"
    role: str = Field(..., description="The authenticated user's role, e.g. 'admin' or 'analyst'.")


class UserResponse(BaseModel):
    """Response for GET /auth/me."""

    username: str
    role: str
