"""Synthetic data generation for the Single View of Wealth (SVW) demo.

Generates a reproducible (seeded) population of people, simulates how
five subsidiaries would *independently* and *imperfectly* record those
same people (name variants, address abbreviations, email-format
differences, missing fields) - both in their payroll feeds and in their
banking-product holdings (current accounts, savings, investments,
mortgages), which carry the same kind of noisy identity capture rather
than a clean, ground-truth attachment. Every kind of record - payroll and
all four banking-product types - is persisted into one unified `records`
table, distinguished by its `record_type` column.

Nothing here is real data - it is all Faker-generated - but the shapes
and the data-quality issues mirror what a real banking group sees when
consolidating feeds from subsidiaries that have never agreed on a common
employee identifier.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from faker import Faker

from app.models import ProductBundle, RecordType, Subsidiary

# ---------------------------------------------------------------------------
# Reproducibility & scale constants
# ---------------------------------------------------------------------------
SEED = 6999
N_PEOPLE = 10_000
SUBSIDIARIES = [s.value for s in Subsidiary]
# Short, stable per-subsidiary code (the enum member name, e.g. "A") used as
# the employee_id prefix - independent of the subsidiary's display name.
SUBSIDIARY_CODES = {s.value: s.name for s in Subsidiary}

# Each person appears in exactly one subsidiary's payroll feed:
#   10,000 people x 1 record = 10,000 payroll records
RECORD_COUNT_DISTRIBUTION = {1: 10_000}
N_RECORDS = sum(count * n for n, count in RECORD_COUNT_DISTRIBUTION.items())

# Banking-product bundle distribution (must sum to N_PEOPLE)
PRODUCT_BUNDLE_DISTRIBUTION = {
    ProductBundle.ONLY_PAYROLL: 5000,
    ProductBundle.PAYROLL_DEPOSITS: 1500,
    ProductBundle.PAYROLL_INVESTMENTS: 1000,
    ProductBundle.PAYROLL_MORTGAGE: 1000,
    ProductBundle.ALL_PRODUCTS: 1500,
}

# Weights (not exact totals - the population each product type applies to is
# itself a derived subset of N_PEOPLE, not known up front) for "how many
# accounts of a given product type does a person who holds that product
# have". Most employees keep a single current account / savings pot / ISA /
# mortgage, with a shrinking minority holding more than one - often at a
# different subsidiary, which is exactly the cross-subsidiary diversity a
# real banking group's employee base shows.
ACCOUNT_COUNT_WEIGHTS = {1: 0.75, 2: 0.18, 3: 0.05, 4: 0.02}

# ---------------------------------------------------------------------------
# Demographic & financial distribution constants
# ---------------------------------------------------------------------------
MINIMUM_AGE = 21
MAXIMUM_AGE = 66

# Salary: a shifted lognormal (see `_lognormal` below) plus a small
# per-subsidiary variance applied independently to each payroll record of
# the same person (different subsidiaries record slightly different figures
# for the same underlying job).
SALARY_FLOOR = 30_000.0
SALARY_MEDIAN_ABOVE_FLOOR = 12_000.0
SALARY_SIGMA = 0.8
SALARY_HIGH = 250_000.0
SALARY_SUBSIDIARY_VARIANCE_LOW = 0.95
SALARY_SUBSIDIARY_VARIANCE_HIGH = 1.05

# Bonus: Beta(alpha, beta) skews toward modest payouts (mean ~14% of salary
# at the defaults below), capped at BONUS_PCT_CAP of annual salary.
BONUS_BETA_ALPHA = 2
BONUS_BETA_BETA = 5
BONUS_PCT_CAP = 0.5

# Per-product-type balance distributions (each a shifted lognormal - see
# `_lognormal` below).
CURRENT_ACCOUNT_BALANCE_FLOOR = 50.0
CURRENT_ACCOUNT_BALANCE_MEDIAN_ABOVE_FLOOR = 2_000.0
CURRENT_ACCOUNT_BALANCE_SIGMA = 1.1
CURRENT_ACCOUNT_BALANCE_HIGH = 50_000.0

SAVINGS_BALANCE_FLOOR = 200.0
SAVINGS_BALANCE_MEDIAN_ABOVE_FLOOR = 10_000.0
SAVINGS_BALANCE_SIGMA = 1.1
SAVINGS_BALANCE_HIGH = 200_000.0

INVESTMENT_BALANCE_FLOOR = 500.0
INVESTMENT_BALANCE_MEDIAN_ABOVE_FLOOR = 30_000.0
INVESTMENT_BALANCE_SIGMA = 1.2
INVESTMENT_BALANCE_HIGH = 500_000.0

# Mortgage balance: salary x a triangular multiplier (peaking at the
# affordability-typical mode), clamped to a realistic UK range.
MORTGAGE_MULTIPLIER_LOW = 2.0
MORTGAGE_MULTIPLIER_HIGH = 6.0
MORTGAGE_MULTIPLIER_MODE = 3.5
MORTGAGE_BALANCE_FLOOR = 50_000.0
MORTGAGE_BALANCE_CAP = 600_000.0

# Identity-noise thresholds shared by every record-emitting call site
# (payroll and all four product types) via `_noisy_identity_capture`. Each
# constant is named for, and gates, the branch it literally selects (e.g.
# `*_UNCHANGED_PROB`/`*_REUSE_PROB` gate the "leave it as-is" branch) so a
# future edit can't accidentally invert its sense. The first-name/last-name
# thresholds are *cumulative* - each `_vary_*` function draws one random
# number and compares it against these in sequence - not independent
# per-branch probabilities.
FIRST_NAME_UNCHANGED_PROB = 0.55
FIRST_NAME_NICKNAME_PROB = 0.80
FIRST_NAME_INITIAL_PROB = 0.92
LAST_NAME_UNCHANGED_PROB = 0.85
LAST_NAME_CASE_VARIANT_PROB = 0.93
ADDRESS_ABBREVIATION_PROB = 0.5  # per street-type token
POSTCODE_STRIP_SPACE_PROB = 0.3
EMAIL_REUSE_PROB = 0.5  # probability of reusing the canonical email verbatim
EMAIL_NUMERIC_SUFFIX_PROB = 0.2
PHONE_REUSE_PROB = 0.5  # probability of reusing the canonical phone verbatim
DOB_UNCHANGED_PROB = 0.95
EMAIL_NULL_PROB = 0.12
PHONE_NULL_PROB = 0.12
ADDRESS_NULL_PROB = 0.10

DB_PATH = Path(os.environ.get("SVW_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "svow.duckdb"))

EMAIL_DOMAINS = ["gmail.com", "outlook.com", "yahoo.co.uk", "hotmail.com", "icloud.com"]

# A representative (not exhaustive) set of common nickname variants used to
# simulate the "John Smith / Jonathan Smith / Jon Smith" style of duplicate.
NICKNAMES: dict[str, list[str]] = {
    "james": ["Jim", "Jimmy"],
    "john": ["Jon", "Johnny"],
    "jonathan": ["Jon", "Jonny"],
    "robert": ["Rob", "Bob", "Bobby"],
    "william": ["Will", "Bill", "Billy"],
    "richard": ["Rick", "Dick", "Rich"],
    "michael": ["Mike", "Mick"],
    "elizabeth": ["Liz", "Beth", "Eliza"],
    "katherine": ["Kate", "Kathy", "Kat"],
    "margaret": ["Maggie", "Meg", "Peggy"],
    "thomas": ["Tom", "Tommy"],
    "charles": ["Charlie", "Chuck"],
    "christopher": ["Chris"],
    "daniel": ["Dan", "Danny"],
    "matthew": ["Matt"],
    "anthony": ["Tony"],
    "patricia": ["Pat", "Patty", "Trish"],
    "jennifer": ["Jen", "Jenny"],
    "samuel": ["Sam", "Sammy"],
    "alexander": ["Alex"],
    "benjamin": ["Ben", "Benny"],
    "nicholas": ["Nick"],
    "andrew": ["Andy", "Drew"],
    "joseph": ["Joe", "Joey"],
    "edward": ["Ed", "Eddie", "Ted"],
    "stephanie": ["Steph"],
    "rebecca": ["Becky", "Becca"],
    "victoria": ["Vicky", "Tori"],
    "deborah": ["Debbie", "Deb"],
    "susan": ["Sue", "Susie"],
    "timothy": ["Tim", "Timmy"],
    "gregory": ["Greg"],
    "kenneth": ["Ken", "Kenny"],
    "donald": ["Don", "Donnie"],
    "frederick": ["Fred", "Freddie"],
    "barbara": ["Barb", "Babs"],
    "alfred": ["Alf", "Alfie"],
}

# UK address-component abbreviations used to simulate the
# "10 Main Street" vs "10 Main St" class of duplicate.
ADDRESS_ABBREVIATIONS = {
    "Street": "St",
    "Road": "Rd",
    "Avenue": "Ave",
    "Lane": "Ln",
    "Drive": "Dr",
    "Court": "Ct",
    "Place": "Pl",
    "Square": "Sq",
    "Crescent": "Cres",
    "Gardens": "Gdns",
    "Close": "Cl",
    "Terrace": "Ter",
    "Grove": "Gr",
    "Park": "Pk",
}

# Column order for the unified `records` table - payroll-only fields
# (`employee_id`, `annual_salary`, `bonus`) and product-only fields
# (`account_id`, `balance`) are nullable, populated only for the relevant
# `record_type`.
RECORD_COLUMNS = [
    "source_record_id",
    "person_index",
    "subsidiary",
    "record_type",
    "employee_id",
    "account_id",
    "first_name",
    "last_name",
    "date_of_birth",
    "email",
    "phone",
    "address",
    "city",
    "postcode",
    "annual_salary",
    "bonus",
    "balance",
    "currency",
]


def get_connection() -> duckdb.DuckDBPyConnection:
    """Open a fresh connection to the on-disk DuckDB store.

    DuckDB file connections are cheap to open/close and only support a
    single writer at a time; opening a short-lived connection per
    request (rather than holding one open for the app's lifetime) keeps
    the demo simple and avoids cross-request lock contention.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def _vary_first_name(first_name: str, rng: random.Random) -> str:
    """Return a noisy variant of a first name: full / nickname / initial / case."""
    key = first_name.lower()
    roll = rng.random()
    if roll < FIRST_NAME_UNCHANGED_PROB:
        return first_name
    if key in NICKNAMES and roll < FIRST_NAME_NICKNAME_PROB:
        return rng.choice(NICKNAMES[key])
    if roll < FIRST_NAME_INITIAL_PROB:
        return first_name[0]
    return first_name.upper() if rng.random() < 0.5 else first_name.lower()


def _vary_last_name(last_name: str, rng: random.Random) -> str:
    """Return a noisy variant of a surname: mostly unchanged, occasional case/typo."""
    roll = rng.random()
    if roll < LAST_NAME_UNCHANGED_PROB:
        return last_name
    if roll < LAST_NAME_CASE_VARIANT_PROB:
        return last_name.upper() if rng.random() < 0.5 else last_name.lower()
    # Single-character transcription typo (swap two adjacent letters).
    if len(last_name) > 3:
        i = rng.randrange(1, len(last_name) - 1)
        chars = list(last_name)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        return "".join(chars)
    return last_name


def _vary_address(address: str, rng: random.Random) -> str:
    """Abbreviate street-type tokens with ~ADDRESS_ABBREVIATION_PROB probability per occurrence."""
    out = address
    for full, abbr in ADDRESS_ABBREVIATIONS.items():
        if full in out and rng.random() < ADDRESS_ABBREVIATION_PROB:
            out = out.replace(full, abbr)
    return out


def _vary_postcode(postcode: str, rng: random.Random) -> str:
    """UK postcodes are sometimes typed without the internal space."""
    if rng.random() < POSTCODE_STRIP_SPACE_PROB:
        return postcode.replace(" ", "")
    return postcode


def _make_email(first_name: str, last_name: str, rng: random.Random) -> str:
    domain = rng.choice(EMAIL_DOMAINS)
    style = rng.choice(["dot", "nodot", "initial", "underscore"])
    f, l = first_name.lower(), last_name.lower()
    if style == "dot":
        local = f"{f}.{l}"
    elif style == "nodot":
        local = f"{f}{l}"
    elif style == "initial":
        local = f"{f[0]}{l}"
    else:
        local = f"{f}_{l}"
    if rng.random() < EMAIL_NUMERIC_SUFFIX_PROB:
        local += str(rng.randint(1, 99))
    return f"{local}@{domain}"


def _vary_email(canonical_email: str, first_name: str, last_name: str, rng: random.Random) -> str:
    """Either reuse the canonical address verbatim, or regenerate a same-person
    variant under a different formatting convention (simulating a different
    subsidiary's email-issuing system)."""
    if rng.random() < EMAIL_REUSE_PROB:
        return canonical_email
    return _make_email(first_name, last_name, rng)


def _vary_phone(canonical_phone: str, rng: random.Random) -> str:
    """Either reuse the canonical number, or strip formatting characters
    (spaces/brackets/dashes) to simulate a different system's storage format."""
    if rng.random() < PHONE_REUSE_PROB:
        return canonical_phone
    return "".join(ch for ch in canonical_phone if ch not in " ()-")


def _vary_dob(iso_date: str, rng: random.Random) -> str:
    """Introduce an occasional realistic data-entry error: day/month transposed."""
    if rng.random() < DOB_UNCHANGED_PROB:
        return iso_date
    year, month, day = iso_date.split("-")
    if day <= "12":
        return f"{year}-{day}-{month}"
    return iso_date


def _noisy_identity_capture(person: dict, rng: random.Random) -> dict:
    """Independently capture one subsidiary system's noisy snapshot of a
    person's identity - the same noise model originally inlined in the
    payroll loop, factored out so every record-emitting call site (payroll
    and all four product types) shares one seven-field noise/nulling path."""
    first_name_noisy = _vary_first_name(person["first_name"], rng)
    last_name_noisy = _vary_last_name(person["last_name"], rng)
    dob_noisy = _vary_dob(person["date_of_birth"], rng)
    address_noisy = _vary_address(person["address"], rng)
    postcode_noisy = _vary_postcode(person["postcode"], rng)
    email_noisy = _vary_email(person["email"], person["first_name"], person["last_name"], rng)
    phone_noisy = _vary_phone(person["phone"], rng)

    # Missing-value simulation: independently null out phone/email/address.
    return {
        "first_name": first_name_noisy,
        "last_name": last_name_noisy,
        "date_of_birth": dob_noisy,
        "email": None if rng.random() < EMAIL_NULL_PROB else email_noisy,
        "phone": None if rng.random() < PHONE_NULL_PROB else phone_noisy,
        "address": None if rng.random() < ADDRESS_NULL_PROB else address_noisy,
        "city": person["city"],
        "postcode": postcode_noisy,
    }


def _lognormal(rng: random.Random, floor: float, median_above_floor: float, sigma: float, high: float) -> float:
    """Sample a right-skewed value: `floor` plus a lognormal-distributed
    excess over it (median `median_above_floor`, spread `sigma`), capped at
    `high`. Salaries and account balances are not uniformly distributed in
    reality - most sit well below the maximum, tapering smoothly down toward
    a floor with a shrinking population stretching out toward the top.

    A *shifted* lognormal (rather than a plain lognormal clamped at the
    floor) matters here: clamping piles up a spike of values sitting exactly
    on the floor, which looks obviously synthetic in a histogram; shifting
    means the floor is simply unreachable, with density tapering toward it.
    """
    return min(high, floor + rng.lognormvariate(math.log(median_above_floor), sigma))


def _shuffled_record_counts(rng: random.Random) -> list[int]:
    counts: list[int] = []
    for n, how_many in RECORD_COUNT_DISTRIBUTION.items():
        counts.extend([n] * how_many)
    rng.shuffle(counts)
    return counts


def _shuffled_product_bundles(rng: random.Random) -> list[ProductBundle]:
    bundles: list[ProductBundle] = []
    for bundle, how_many in PRODUCT_BUNDLE_DISTRIBUTION.items():
        bundles.extend([bundle] * how_many)
    rng.shuffle(bundles)
    return bundles


def _account_subsidiaries(rng: random.Random) -> list[str]:
    """Pick which (distinct) subsidiaries hold one person's accounts of a
    given product type: usually just one, occasionally a small handful
    spread across the group's subsidiaries."""
    n = rng.choices(list(ACCOUNT_COUNT_WEIGHTS), weights=list(ACCOUNT_COUNT_WEIGHTS.values()), k=1)[0]
    return rng.sample(SUBSIDIARIES, min(n, len(SUBSIDIARIES)))


@dataclass
class GenerationResult:
    people: int
    records: int
    persons_df: pd.DataFrame = field(repr=False)
    records_df: pd.DataFrame = field(repr=False)


def generate_all(seed: int = SEED) -> GenerationResult:
    """Generate the full synthetic dataset and persist it to DuckDB.

    Returns the row counts plus the generated frames (handy for the demo
    script, which wants to show "before Splink" duplicates without a
    second DB round-trip).
    """
    rng = random.Random(seed)
    faker = Faker("en_GB")
    Faker.seed(seed)

    # --- 1. Ground-truth persons -----------------------------------------
    persons: list[dict] = []
    for idx in range(N_PEOPLE):
        first_name = faker.first_name()
        last_name = faker.last_name()
        dob = faker.date_of_birth(minimum_age=MINIMUM_AGE, maximum_age=MAXIMUM_AGE)
        address = faker.street_address().replace("\n", ", ")
        city = faker.city()
        postcode = faker.postcode()
        email = _make_email(first_name, last_name, rng)
        phone = faker.phone_number()
        persons.append(
            {
                "person_index": idx,
                "first_name": first_name,
                "last_name": last_name,
                "date_of_birth": dob.isoformat(),
                "email": email,
                "phone": phone,
                "address": address,
                "city": city,
                "postcode": postcode,
            }
        )
    persons_df = pd.DataFrame(persons)

    # --- 2. Noisy multi-subsidiary payroll records -------------------------
    record_counts = _shuffled_record_counts(rng)
    assert len(record_counts) == N_PEOPLE

    source_records: list[dict] = []
    record_seq = 0
    for idx, person in enumerate(persons):
        n_records = record_counts[idx]
        chosen_subsidiaries = rng.sample(SUBSIDIARIES, n_records)
        base_salary = _lognormal(
            rng, floor=SALARY_FLOOR, median_above_floor=SALARY_MEDIAN_ABOVE_FLOOR, sigma=SALARY_SIGMA, high=SALARY_HIGH
        )

        for subsidiary in chosen_subsidiaries:
            record_seq += 1
            source_record_id = f"REC{record_seq:06d}"
            employee_id = f"{SUBSIDIARY_CODES[subsidiary]}{rng.randint(100000, 999999)}"

            identity = _noisy_identity_capture(person, rng)

            salary = max(
                SALARY_FLOOR,
                min(SALARY_HIGH, base_salary * rng.uniform(SALARY_SUBSIDIARY_VARIANCE_LOW, SALARY_SUBSIDIARY_VARIANCE_HIGH)),
            )
            # Beta(alpha, beta) skews bonus toward modest payouts with a
            # shrinking tail up to the cap, rather than every employee being
            # equally likely to land anywhere in the range.
            bonus_pct = rng.betavariate(BONUS_BETA_ALPHA, BONUS_BETA_BETA) * BONUS_PCT_CAP

            source_records.append(
                {
                    "source_record_id": source_record_id,
                    "person_index": idx,
                    "subsidiary": subsidiary,
                    "record_type": RecordType.PAYROLL.value,
                    "employee_id": employee_id,
                    **identity,
                    "annual_salary": round(salary, 2),
                    "bonus": round(salary * bonus_pct, 2),
                    "currency": "GBP",
                }
            )

    source_records_df = pd.DataFrame(source_records)
    assert len(source_records_df) == N_RECORDS

    # --- 3. Banking products (noisy, identity-bearing, Splink-resolved) ----
    bundles = _shuffled_product_bundles(rng)
    assert len(bundles) == N_PEOPLE

    # Mean salary per person, precomputed once rather than re-filtering
    # source_records_df from scratch inside the loop below.
    salary_by_person = source_records_df.groupby("person_index")["annual_salary"].mean()

    product_records: list[dict] = []
    product_seqs = {"CA": 0, "SA": 0, "IV": 0, "MG": 0}

    def _next_id(prefix: str) -> str:
        product_seqs[prefix] += 1
        return f"{prefix}{product_seqs[prefix]:06d}"

    def _emit_product(idx: int, record_type: RecordType, account_prefix: str, subsidiary: str, balance: float) -> None:
        nonlocal record_seq
        record_seq += 1
        product_records.append(
            {
                "source_record_id": f"REC{record_seq:06d}",
                "person_index": idx,
                "subsidiary": subsidiary,
                "record_type": record_type.value,
                "account_id": _next_id(account_prefix),
                **_noisy_identity_capture(persons[idx], rng),
                "balance": round(balance, 2),
                "currency": "GBP",
            }
        )

    for idx, bundle in enumerate(bundles):
        mean_salary = salary_by_person.get(idx)
        person_salary = (
            float(mean_salary)
            if mean_salary is not None
            else _lognormal(rng, SALARY_FLOOR, SALARY_MEDIAN_ABOVE_FLOOR, SALARY_SIGMA, SALARY_HIGH)
        )

        has_deposits = bundle in (ProductBundle.PAYROLL_DEPOSITS, ProductBundle.ALL_PRODUCTS)
        has_investments = bundle in (ProductBundle.PAYROLL_INVESTMENTS, ProductBundle.ALL_PRODUCTS)
        has_mortgage = bundle in (ProductBundle.PAYROLL_MORTGAGE, ProductBundle.ALL_PRODUCTS)

        if has_deposits:
            # Current/savings balances are heavily right-skewed in reality -
            # most people keep modest buffers, a shrinking few keep much more.
            # A person may hold more than one account of the same type,
            # often at a different subsidiary - each gets its own balance
            # draw rather than splitting a single total, and its own
            # independent noisy identity capture (a different subsidiary
            # system recording the same person imperfectly).
            for subsidiary in _account_subsidiaries(rng):
                _emit_product(
                    idx,
                    RecordType.CURRENT_ACCOUNT,
                    "CA",
                    subsidiary,
                    _lognormal(
                        rng,
                        CURRENT_ACCOUNT_BALANCE_FLOOR,
                        CURRENT_ACCOUNT_BALANCE_MEDIAN_ABOVE_FLOOR,
                        CURRENT_ACCOUNT_BALANCE_SIGMA,
                        CURRENT_ACCOUNT_BALANCE_HIGH,
                    ),
                )
            for subsidiary in _account_subsidiaries(rng):
                _emit_product(
                    idx,
                    RecordType.SAVINGS_ACCOUNT,
                    "SA",
                    subsidiary,
                    _lognormal(
                        rng, SAVINGS_BALANCE_FLOOR, SAVINGS_BALANCE_MEDIAN_ABOVE_FLOOR, SAVINGS_BALANCE_SIGMA, SAVINGS_BALANCE_HIGH
                    ),
                )
        if has_investments:
            for subsidiary in _account_subsidiaries(rng):
                _emit_product(
                    idx,
                    RecordType.INVESTMENT,
                    "IV",
                    subsidiary,
                    _lognormal(
                        rng,
                        INVESTMENT_BALANCE_FLOOR,
                        INVESTMENT_BALANCE_MEDIAN_ABOVE_FLOOR,
                        INVESTMENT_BALANCE_SIGMA,
                        INVESTMENT_BALANCE_HIGH,
                    ),
                )
        if has_mortgage:
            # Mortgages scale with salary (affordability), within realistic UK
            # bounds. A triangular distribution peaking at ~3.5x income
            # reflects that most mortgages cluster around typical affordability
            # multiples, with fewer people stretching to 2x or 6x. People with
            # more than one mortgage (a small minority) draw an independent
            # multiplier per mortgage rather than splitting one affordability
            # budget across them - a known, accepted simplification.
            for subsidiary in _account_subsidiaries(rng):
                mortgage_multiplier = rng.triangular(MORTGAGE_MULTIPLIER_LOW, MORTGAGE_MULTIPLIER_HIGH, MORTGAGE_MULTIPLIER_MODE)
                mortgage_balance = max(MORTGAGE_BALANCE_FLOOR, min(MORTGAGE_BALANCE_CAP, person_salary * mortgage_multiplier))
                _emit_product(idx, RecordType.MORTGAGE, "MG", subsidiary, mortgage_balance)

    product_records_df = pd.DataFrame(product_records)

    # --- 4. Unify and persist everything to DuckDB --------------------------
    all_records_df = pd.concat([source_records_df, product_records_df], ignore_index=True)[RECORD_COLUMNS]

    conn = get_connection()
    try:
        _persist(conn, "persons", persons_df)
        _persist(conn, "records", all_records_df)
        # Drop the legacy tables this schema replaces (the old two-table
        # split, plus earlier per-product-type tables), so a pre-existing
        # data/svow.duckdb doesn't accumulate orphaned tables.
        for legacy_table in (
            "source_records",
            "product_records",
            "current_accounts",
            "savings_accounts",
            "investments",
            "mortgages",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        # Downstream linkage/wealth tables are now stale - drop them so the
        # API can detect "linkage hasn't been (re)run since the last
        # generation" rather than serving results against old data.
        for stale_table in ("clusters", "wealth_profiles"):
            conn.execute(f"DROP TABLE IF EXISTS {stale_table}")
    finally:
        conn.close()

    return GenerationResult(
        people=len(persons_df),
        records=len(all_records_df),
        persons_df=persons_df,
        records_df=all_records_df,
    )


def _persist(conn: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame) -> None:
    view_name = f"_{table_name}_incoming"
    conn.register(view_name, df)
    conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM {view_name}")
    conn.unregister(view_name)


def has_generated_data() -> bool:
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        return "records" in tables
    finally:
        conn.close()
