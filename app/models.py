"""Domain models for the Single View of Wealth (SVW) platform.

These Pydantic models describe the core entities that flow through the
system: the people the bank's group employs, the noisy per-subsidiary
records about them (payroll and banking-product holdings alike, unified
into one `Record` shape), and the results produced by the Splink
entity-resolution pipeline. They are used internally by the services (data
generation, linkage, wealth aggregation) and double up as a single source
of truth for the shape of each DuckDB table.

`schemas.py` builds the public API request/response contracts on top of
these, so the two files stay in sync without duplicating field semantics.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Subsidiary(str, Enum):
    """The five payroll-providing subsidiaries in the corporate group.

    Member names (A-E) double as short, stable codes used for employee_id
    generation in `data_generator.py`; member values are the display names
    shown throughout the API and frontend.
    """

    A = "Hawksworth Retail Bank"
    B = "Calder Wealth Partners"
    C = "Brightfield Trust"
    D = "Sterling Commercial Bank"
    E = "Ridgeway Private Bank"


class ProductBundle(str, Enum):
    """The banking-product ownership pattern assigned to a person.

    Used only during synthetic data generation to guarantee the demo
    covers every combination called out in the spec (payroll-only
    employees, deposit-only employees, etc.).
    """

    ONLY_PAYROLL = "only_payroll"
    PAYROLL_DEPOSITS = "payroll_deposits"
    PAYROLL_INVESTMENTS = "payroll_investments"
    PAYROLL_MORTGAGE = "payroll_mortgage"
    ALL_PRODUCTS = "all_products"


class Person(BaseModel):
    """A ground-truth individual, as known only to the data generator.

    In reality a bank never observes this clean record directly - it is
    reconstructed (imperfectly) by Splink from the noisy `Record` rows. We
    persist it purely so the demo can show "before/after" and compute
    linkage-quality metrics; production systems would not have this table.
    """

    person_index: int
    first_name: str
    last_name: str
    date_of_birth: date
    email: str
    phone: str
    address: str
    city: str
    postcode: str


class RecordType(str, Enum):
    """The five kinds of noisy record the unified `records` table can hold:
    one payroll kind plus the four banking-product kinds. Member values are
    uppercase and equal to the member name, matching the `record_type`
    column's literal values in DuckDB."""

    PAYROLL = "PAYROLL"
    CURRENT_ACCOUNT = "CURRENT_ACCOUNT"
    SAVINGS_ACCOUNT = "SAVINGS_ACCOUNT"
    INVESTMENT = "INVESTMENT"
    MORTGAGE = "MORTGAGE"


class Record(BaseModel):
    """A single subsidiary system's noisy record of one person - either a
    payroll record or a banking-product holding, distinguished by
    `record_type`. This is the unit of record Splink deduplicates: one row
    per (person, subsidiary, record_type) capture, carrying the 7 noisy
    identity comparison columns plus whichever payload fields apply to its
    kind. Payroll-only fields (`employee_id`, `annual_salary`, `bonus`) and
    product-only fields (`account_id`, `balance`) are nullable, populated
    only for the relevant `record_type`.
    """

    model_config = ConfigDict(use_enum_values=True)

    source_record_id: str
    person_index: int = Field(..., description="Ground-truth person id; hidden from Splink.")
    subsidiary: Subsidiary
    record_type: RecordType
    employee_id: str | None = None
    account_id: str | None = None
    first_name: str
    last_name: str
    date_of_birth: str = Field(..., description="ISO date string; may carry data-entry noise.")
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    postcode: str | None = None
    annual_salary: float | None = None
    bonus: float | None = None
    balance: float | None = None
    currency: str


class ClusterAssignment(BaseModel):
    """One row of Splink's clustering output, as persisted to DuckDB.

    `match_probability` is not a native per-record Splink output (Splink
    scores pairwise edges, not records) - see `splink_service.py` for how
    it is derived as a per-record confidence score.
    """

    source_record_id: str
    master_person_id: str
    match_probability: float


class WealthProfile(BaseModel):
    """The aggregated "golden" employee profile for one resolved person."""

    master_person_id: str
    name: str
    salary: float
    cash: float
    savings: float
    investments: float
    mortgage: float
    net_wealth: float
