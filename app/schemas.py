"""API request/response schemas (the FastAPI/OpenAPI contract layer).

Kept separate from `models.py` so the internal domain shape (DuckDB
tables) can evolve independently of what the API promises callers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GenerateDataResponse(BaseModel):
    """Response for POST /generate-data."""

    people: int = Field(..., description="Number of unique ground-truth people generated.")
    records: int = Field(..., description="Total noisy records (payroll + banking product) generated across all subsidiaries.")


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
    source_records: int = Field(..., description="Total noisy records (payroll + banking product) ingested from subsidiaries.")
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
    """One subsidiary's raw noisy record, as linked into a resolved profile -
    payroll or banking-product alike, distinguished by `record_type`. This
    is a genuinely Splink-resolved cluster member; `match_probability` is
    this record's own per-record linkage confidence, not a trusted
    ground-truth attachment. Payroll-only fields (`employee_id`,
    `annual_salary`, `bonus`) and product-only fields (`account_id`,
    `balance`) are nullable, populated only for the relevant `record_type`.
    A person may hold several records of the same `record_type` (e.g.
    multiple current accounts), each tagged with the subsidiary it sits at."""

    source_record_id: str
    subsidiary: str
    record_type: str = Field(
        ..., description="One of 'PAYROLL', 'CURRENT_ACCOUNT', 'SAVINGS_ACCOUNT', 'INVESTMENT', 'MORTGAGE'."
    )
    employee_id: str | None
    account_id: str | None
    first_name: str
    last_name: str
    date_of_birth: str
    email: str | None
    phone: str | None
    address: str | None
    city: str | None
    postcode: str | None
    annual_salary: float | None
    bonus: float | None
    balance: float | None
    currency: str
    match_probability: float = Field(..., description="This record's own per-record linkage confidence.")


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
    record_count: int = Field(..., description="Number of linked PAYROLL records specifically (not banking products).")
    linked_subsidiaries: list[str] = Field(..., description="Subsidiaries with a linked PAYROLL record for this profile.")
    primary_city: str | None
    primary_postcode: str | None
    records: list[LinkedRecord] = Field(
        ..., description="Every record linked into this profile - payroll and banking-product alike."
    )
    field_agreement: list[FieldAgreement] = Field(
        ..., description="Field-by-field agreement across every linked record (payroll and banking-product)."
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
    total_clusters: int = Field(..., description="Total resolved master_person_id clusters in the current dataset.")
    avg_match_probability: float = Field(..., description="Mean per-record linkage confidence across all clusters.")
    multi_record_cluster_count: int = Field(
        ..., description="Clusters with 2 or more linked records of any type - i.e. genuinely deduplicated identities."
    )
    high_confidence_pct: float = Field(
        ..., description="Percentage of linked records with match probability >= 0.99."
    )


class SegmentSummary(BaseModel):
    """One net-worth tier's aggregate stats - one card on the Employee
    Segments page."""

    wealth_tier: str = Field(..., description="e.g. 'Affluent'.")
    min_net_wealth: float | None = Field(
        None, description="Inclusive lower bound of this tier's net_wealth range, or null for the bottom (unbounded) tier."
    )
    max_net_wealth: float | None = Field(
        None, description="Exclusive upper bound of this tier's net_wealth range, or null for the top (unbounded) tier."
    )
    employee_count: int
    pct_of_population: float = Field(..., description="This tier's share of all resolved profiles, as a percentage.")
    total_net_wealth: float
    avg_net_wealth: float
    avg_salary: float
    avg_savings: float


class SegmentationResponse(BaseModel):
    """Response for GET /segmentation."""

    total_profiles: int
    segments: list[SegmentSummary]


class SegmentMembersResponse(BaseModel):
    """Response for GET /segmentation/{tier}/members."""

    wealth_tier: str
    results: list[SearchResultItem]
    total: int = Field(..., description="Total members of this tier - may exceed len(results) once limit kicks in.")


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
