"""Synthetic data generation for the Single View of Wealth (SVW) demo.

Generates a reproducible (seeded) population of people, simulates how
five subsidiaries would *independently* and *imperfectly* record those
same people (name variants, address abbreviations, email-format
differences, missing fields) - both in their payroll feeds and in their
banking-product holdings (current accounts, savings, investments,
mortgages), which carry the same kind of noisy identity capture rather
than a clean, ground-truth attachment.

Nothing here is real data - it is all Faker-generated - but the shapes
and the data-quality issues mirror what a real banking group sees when
consolidating feeds from subsidiaries that have never agreed on a common
customer or employee identifier.
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
SEED = 777
N_PEOPLE = 10_000
SUBSIDIARIES = [s.value for s in Subsidiary]
# Short, stable per-subsidiary code (the enum member name, e.g. "A") used as
# the employee_id prefix - independent of the subsidiary's display name.
SUBSIDIARY_CODES = {s.value: s.name for s in Subsidiary}

# Distribution of "how many subsidiaries recorded this person" chosen so the
# totals are *exact*, not just approximately on target:
#   2,000 people x 1 record + 3,000 x 2 + 3,000 x 3 + 2,000 x 4
#   = 10,000 people, 25,000 records
RECORD_COUNT_DISTRIBUTION = {1: 2000, 2: 3000, 3: 3000, 4: 2000}
N_RECORDS = sum(count * n for n, count in RECORD_COUNT_DISTRIBUTION.items())

# Banking-product bundle distribution (must sum to N_PEOPLE)
PRODUCT_BUNDLE_DISTRIBUTION = {
    ProductBundle.ONLY_PAYROLL: 2000,
    ProductBundle.PAYROLL_DEPOSITS: 2500,
    ProductBundle.PAYROLL_INVESTMENTS: 1500,
    ProductBundle.PAYROLL_MORTGAGE: 1500,
    ProductBundle.ALL_PRODUCTS: 2500,
}

# Weights (not exact totals - the population each product type applies to is
# itself a derived subset of N_PEOPLE, not known up front) for "how many
# accounts of a given product type does a person who holds that product
# have". Most customers keep a single current account / savings pot / ISA /
# mortgage, with a shrinking minority holding more than one - often at a
# different subsidiary, which is exactly the cross-subsidiary diversity a
# real banking group's customer base shows.
ACCOUNT_COUNT_WEIGHTS = {1: 0.75, 2: 0.18, 3: 0.05, 4: 0.02}

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
    if roll < 0.55:
        return first_name
    if key in NICKNAMES and roll < 0.80:
        return rng.choice(NICKNAMES[key])
    if roll < 0.92:
        return first_name[0]
    return first_name.upper() if rng.random() < 0.5 else first_name.lower()


def _vary_last_name(last_name: str, rng: random.Random) -> str:
    """Return a noisy variant of a surname: mostly unchanged, occasional case/typo."""
    roll = rng.random()
    if roll < 0.85:
        return last_name
    if roll < 0.93:
        return last_name.upper() if rng.random() < 0.5 else last_name.lower()
    # Single-character transcription typo (swap two adjacent letters).
    if len(last_name) > 3:
        i = rng.randrange(1, len(last_name) - 1)
        chars = list(last_name)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        return "".join(chars)
    return last_name


def _vary_address(address: str, rng: random.Random) -> str:
    """Abbreviate street-type tokens with ~50% probability per occurrence."""
    out = address
    for full, abbr in ADDRESS_ABBREVIATIONS.items():
        if full in out and rng.random() < 0.5:
            out = out.replace(full, abbr)
    return out


def _vary_postcode(postcode: str, rng: random.Random) -> str:
    """UK postcodes are sometimes typed without the internal space."""
    if rng.random() < 0.3:
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
    if rng.random() < 0.2:
        local += str(rng.randint(1, 99))
    return f"{local}@{domain}"


def _vary_email(canonical_email: str, first_name: str, last_name: str, rng: random.Random) -> str:
    """Either reuse the canonical address verbatim, or regenerate a same-person
    variant under a different formatting convention (simulating a different
    subsidiary's email-issuing system)."""
    if rng.random() < 0.5:
        return canonical_email
    return _make_email(first_name, last_name, rng)


def _vary_phone(canonical_phone: str, rng: random.Random) -> str:
    """Either reuse the canonical number, or strip formatting characters
    (spaces/brackets/dashes) to simulate a different system's storage format."""
    if rng.random() < 0.5:
        return canonical_phone
    return "".join(ch for ch in canonical_phone if ch not in " ()-")


def _vary_dob(iso_date: str, rng: random.Random) -> str:
    """Introduce an occasional realistic data-entry error: day/month transposed."""
    if rng.random() < 0.95:
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
        "email": None if rng.random() < 0.12 else email_noisy,
        "phone": None if rng.random() < 0.12 else phone_noisy,
        "address": None if rng.random() < 0.10 else address_noisy,
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
    source_records_df: pd.DataFrame = field(repr=False)


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
        dob = faker.date_of_birth(minimum_age=21, maximum_age=66)
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

    # --- 2. Noisy multi-subsidiary source records -------------------------
    record_counts = _shuffled_record_counts(rng)
    assert len(record_counts) == N_PEOPLE

    source_records: list[dict] = []
    record_seq = 0
    for idx, person in enumerate(persons):
        n_records = record_counts[idx]
        chosen_subsidiaries = rng.sample(SUBSIDIARIES, n_records)
        base_salary = _lognormal(rng, floor=30_000, median_above_floor=12_000, sigma=0.8, high=250_000)

        for subsidiary in chosen_subsidiaries:
            record_seq += 1
            source_record_id = f"REC{record_seq:06d}"
            employee_id = f"{SUBSIDIARY_CODES[subsidiary]}{rng.randint(100000, 999999)}"

            identity = _noisy_identity_capture(person, rng)

            salary = max(30_000.0, min(250_000.0, base_salary * rng.uniform(0.95, 1.05)))
            # Beta(2, 5) skews bonus toward modest payouts (mean ~14% of
            # salary) with a shrinking tail up to the 50% cap, rather than
            # every employee being equally likely to land anywhere 0-50%.
            bonus_pct = rng.betavariate(2, 5) * 0.5

            source_records.append(
                {
                    "source_record_id": source_record_id,
                    "person_index": idx,
                    "subsidiary": subsidiary,
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
    product_seqs = {"PR": 0, "CA": 0, "SA": 0, "IV": 0, "MG": 0}

    def _next_id(prefix: str) -> str:
        product_seqs[prefix] += 1
        return f"{prefix}{product_seqs[prefix]:06d}"

    def _emit_product(idx: int, record_type: RecordType, account_prefix: str, subsidiary: str, balance: float) -> None:
        product_records.append(
            {
                "source_record_id": _next_id("PR"),
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
        person_salary = float(mean_salary) if mean_salary is not None else _lognormal(rng, 30_000, 12_000, 0.8, 250_000)

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
                _emit_product(idx, RecordType.CURRENT_ACCOUNT, "CA", subsidiary, _lognormal(rng, 50, 2_000, 1.1, 50_000))
            for subsidiary in _account_subsidiaries(rng):
                _emit_product(idx, RecordType.SAVINGS_ACCOUNT, "SA", subsidiary, _lognormal(rng, 200, 10_000, 1.1, 200_000))
        if has_investments:
            for subsidiary in _account_subsidiaries(rng):
                _emit_product(idx, RecordType.INVESTMENT, "IV", subsidiary, _lognormal(rng, 500, 30_000, 1.2, 500_000))
        if has_mortgage:
            # Mortgages scale with salary (affordability), within realistic UK
            # bounds. A triangular distribution peaking at ~3.5x income
            # reflects that most mortgages cluster around typical affordability
            # multiples, with fewer people stretching to 2x or 6x. People with
            # more than one mortgage (a small minority) draw an independent
            # multiplier per mortgage rather than splitting one affordability
            # budget across them - a known, accepted simplification.
            for subsidiary in _account_subsidiaries(rng):
                mortgage_multiplier = rng.triangular(2.0, 6.0, 3.5)
                mortgage_balance = max(50_000.0, min(600_000.0, person_salary * mortgage_multiplier))
                _emit_product(idx, RecordType.MORTGAGE, "MG", subsidiary, mortgage_balance)

    product_records_df = pd.DataFrame(product_records)

    # --- 4. Persist everything to DuckDB -----------------------------------
    conn = get_connection()
    try:
        _persist(conn, "persons", persons_df)
        _persist(conn, "source_records", source_records_df)
        _persist(conn, "product_records", product_records_df)
        # Drop the legacy per-product-type tables this schema replaces, so a
        # pre-existing data/svow.duckdb doesn't accumulate orphaned tables.
        for legacy_table in ("current_accounts", "savings_accounts", "investments", "mortgages"):
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
        records=len(source_records_df),
        persons_df=persons_df,
        source_records_df=source_records_df,
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
        return "source_records" in tables
    finally:
        conn.close()
