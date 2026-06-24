"""Synthetic data generation for the Single View of Wealth (SVW) demo.

Generates a reproducible (seeded) population of people, simulates how
five payroll subsidiaries would *independently* and *imperfectly* record
those same people (name variants, address abbreviations, email-format
differences, missing fields), and generates banking-product holdings.

Nothing here is real data - it is all Faker-generated - but the shapes
and the data-quality issues mirror what a real banking group sees when
consolidating payroll feeds from subsidiaries that have never agreed on
a common employee identifier.
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

from app.models import ProductBundle, Subsidiary

# ---------------------------------------------------------------------------
# Reproducibility & scale constants
# ---------------------------------------------------------------------------
SEED = 666
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

            first_name_noisy = _vary_first_name(person["first_name"], rng)
            last_name_noisy = _vary_last_name(person["last_name"], rng)
            dob_noisy = _vary_dob(person["date_of_birth"], rng)
            address_noisy = _vary_address(person["address"], rng)
            postcode_noisy = _vary_postcode(person["postcode"], rng)
            email_noisy = _vary_email(person["email"], person["first_name"], person["last_name"], rng)
            phone_noisy = _vary_phone(person["phone"], rng)

            # Missing-value simulation: independently null out phone/email/address.
            email_final = None if rng.random() < 0.12 else email_noisy
            phone_final = None if rng.random() < 0.12 else phone_noisy
            address_final = None if rng.random() < 0.10 else address_noisy

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
                    "first_name": first_name_noisy,
                    "last_name": last_name_noisy,
                    "date_of_birth": dob_noisy,
                    "email": email_final,
                    "phone": phone_final,
                    "address": address_final,
                    "city": person["city"],
                    "postcode": postcode_noisy,
                    "annual_salary": round(salary, 2),
                    "bonus": round(salary * bonus_pct, 2),
                    "currency": "GBP",
                }
            )

    source_records_df = pd.DataFrame(source_records)
    assert len(source_records_df) == N_RECORDS

    # --- 3. Banking products ----------------------------------------------
    bundles = _shuffled_product_bundles(rng)
    assert len(bundles) == N_PEOPLE

    current_accounts, savings_accounts, investments, mortgages = [], [], [], []
    for idx, bundle in enumerate(bundles):
        salary = source_records_df.loc[source_records_df["person_index"] == idx, "annual_salary"]
        person_salary = (
            float(salary.mean()) if len(salary) else _lognormal(rng, 30_000, 12_000, 0.8, 250_000)
        )

        has_deposits = bundle in (ProductBundle.PAYROLL_DEPOSITS, ProductBundle.ALL_PRODUCTS)
        has_investments = bundle in (ProductBundle.PAYROLL_INVESTMENTS, ProductBundle.ALL_PRODUCTS)
        has_mortgage = bundle in (ProductBundle.PAYROLL_MORTGAGE, ProductBundle.ALL_PRODUCTS)

        if has_deposits:
            # Current/savings balances are heavily right-skewed in reality -
            # most people keep modest buffers, a shrinking few keep much more.
            current_accounts.append(
                {
                    "account_id": f"CA{idx:06d}",
                    "person_index": idx,
                    "account_balance": round(_lognormal(rng, 50, 2_000, 1.1, 50_000), 2),
                }
            )
            savings_accounts.append(
                {
                    "account_id": f"SA{idx:06d}",
                    "person_index": idx,
                    "savings_balance": round(_lognormal(rng, 200, 10_000, 1.1, 200_000), 2),
                }
            )
        if has_investments:
            investments.append(
                {
                    "account_id": f"IV{idx:06d}",
                    "person_index": idx,
                    "investment_balance": round(_lognormal(rng, 500, 30_000, 1.2, 500_000), 2),
                }
            )
        if has_mortgage:
            # Mortgages scale with salary (affordability), within realistic UK
            # bounds. A triangular distribution peaking at ~3.5x income
            # reflects that most mortgages cluster around typical affordability
            # multiples, with fewer people stretching to 2x or 6x.
            mortgage_multiplier = rng.triangular(2.0, 6.0, 3.5)
            mortgage_balance = max(50_000.0, min(600_000.0, person_salary * mortgage_multiplier))
            mortgages.append(
                {
                    "account_id": f"MG{idx:06d}",
                    "person_index": idx,
                    "mortgage_balance": round(mortgage_balance, 2),
                }
            )

    current_accounts_df = pd.DataFrame(current_accounts)
    savings_accounts_df = pd.DataFrame(savings_accounts)
    investments_df = pd.DataFrame(investments)
    mortgages_df = pd.DataFrame(mortgages)

    # --- 4. Persist everything to DuckDB -----------------------------------
    conn = get_connection()
    try:
        _persist(conn, "persons", persons_df)
        _persist(conn, "source_records", source_records_df)
        _persist(conn, "current_accounts", current_accounts_df)
        _persist(conn, "savings_accounts", savings_accounts_df)
        _persist(conn, "investments", investments_df)
        _persist(conn, "mortgages", mortgages_df)
        # Downstream linkage/wealth tables are now stale - drop them so the
        # API can detect "linkage hasn't been (re)run since the last
        # generation" rather than serving results against old data.
        for stale_table in ("clusters", "person_cluster_map", "wealth_profiles"):
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
