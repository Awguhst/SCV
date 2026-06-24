"""Splink-powered entity-resolution service.

This module configures and runs the probabilistic record-linkage model
that turns 25,000 noisy, multi-subsidiary payroll records into a set of
`master_person_id` clusters - one per real individual.

--------------------------------------------------------------------------
Why these Splink configuration choices?
--------------------------------------------------------------------------
* **link_type="dedupe_only"**: all source records live in a single pool
  (there is no second "dataset" to link against) - we are deduplicating
  one combined table, not linking two separate tables.

* **DuckDBAPI backend**: Splink's in-process DuckDB engine is fast enough
  for tens of thousands of rows, requires no external services, and
  keeps the whole POC self-contained (per the brief).

* **Comparisons** map 1:1 onto the seven attributes specified in the
  brief, using Splink's purpose-built comparison templates instead of
  hand-rolled SQL:
    - `NameComparison` for first/last name: handles exact match, a
      Jaro-Winkler tier for typos/case, and a phonetic (double-metaphone)
      fallback - exactly what's needed for "Jon" vs "Jonathan" or a
      transposed-letter surname.
    - `DateOfBirthComparison` parses the ISO date string and scores by
      calendar distance, so a day/month transcription error still scores
      far higher than an unrelated DOB.
    - `EmailComparison` understands the username@domain structure and
      rewards a matching username even when the domain differs (or
      vice-versa) - exactly the "john.smith@gmail.com" vs
      "johnsmith@gmail.com" pattern called out in the brief.
    - `LevenshteinAtThresholds` for phone and address: both are
      formatting-sensitive free-text fields (punctuation, abbreviations)
      where edit distance is a robust, cheap similarity signal.
    - `PostcodeComparison` with term-frequency adjustment: postcodes are
      strong identity signals, but adjustment stops common
      postcode districts from over-crediting unrelated matches.

* **Blocking rules** exist purely for performance: comparing all
  ~25,000^2/2 pairs would be wasteful. Each rule captures a different
  realistic "this is probably the same person" signal (shared email,
  shared phone, name+surname, DOB+postcode, surname+postcode, or
  first-initial+surname+DOB to catch "J Smith" style records), and
  Splink takes the union of all rules' candidate pairs before scoring.

* **Two-stage probability estimation**: a handful of high-precision
  deterministic rules give a sensible starting estimate of how rare a
  random match is, `estimate_u_using_random_sampling` learns how often
  attributes agree *by chance*, and `estimate_parameters_using_expectation_maximisation`
  is run against several different blocking rules so every comparison
  gets trained on data it wasn't blocked on (a standard Splink
  identifiability requirement).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import duckdb
import pandas as pd
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
import splink.comparison_library as cl

from app.data_generator import get_connection

UNIQUE_ID_COL = "source_record_id"

# Predictions below this are dropped before clustering purely to keep the
# pairwise predictions table small; the real decision threshold is
# CLUSTER_MATCH_THRESHOLD below.
PREDICT_MIN_THRESHOLD = 0.05

# A pairwise match probability at/above this becomes a graph edge during
# clustering; two records end up in the same master_person_id cluster iff
# they are connected (directly or transitively) by edges at-or-above this
# threshold. 0.75 was chosen empirically on this synthetic dataset: it is
# comfortably above the "two random people happen to share a surname"
# noise floor while still bridging the heavier name/address noise we
# inject (e.g. initial-only first name + abbreviated address).
CLUSTER_MATCH_THRESHOLD = 0.75

EM_SEED = 42


def _settings() -> SettingsCreator:
    return SettingsCreator(
        link_type="dedupe_only",
        unique_id_column_name=UNIQUE_ID_COL,
        comparisons=[
            cl.NameComparison("first_name"),
            cl.NameComparison("last_name").configure(term_frequency_adjustments=True),
            cl.DateOfBirthComparison(
                "date_of_birth",
                input_is_string=True,
                datetime_format="%Y-%m-%d",
            ),
            cl.EmailComparison("email"),
            cl.LevenshteinAtThresholds("phone", [1, 2]),
            cl.LevenshteinAtThresholds("address", [2, 5, 10]),
            cl.PostcodeComparison("postcode").configure(term_frequency_adjustments=True),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("first_name", "last_name"),
            block_on("email"),
            block_on("phone"),
            block_on("date_of_birth", "postcode"),
            block_on("last_name", "postcode"),
            block_on("substr(first_name,1,1)", "last_name", "date_of_birth"),
        ],
        retain_intermediate_calculation_columns=True,
    )


def _deterministic_rules() -> list[str]:
    """High-precision rules used only to seed the prior probability that two
    random records match. None of these alone decides a final cluster -
    that is what the trained model + clustering threshold do."""
    return [
        "l.email = r.email and l.email is not null",
        "l.phone = r.phone and l.phone is not null and l.last_name = r.last_name",
        "l.date_of_birth = r.date_of_birth and l.postcode = r.postcode and l.last_name = r.last_name",
    ]


def _load_source_records(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return conn.execute("SELECT * FROM source_records").df()


def _build_and_train_linker(df: pd.DataFrame) -> Linker:
    linker = Linker(df, _settings(), db_api=DuckDBAPI())

    linker.training.estimate_probability_two_random_records_match(
        _deterministic_rules(), recall=0.6
    )
    linker.training.estimate_u_using_random_sampling(max_pairs=2e6, seed=EM_SEED)

    # Train m-probabilities against several different blocking rules so
    # every comparison vector level gets observed outside the rule it was
    # blocked on (Splink's standard EM identifiability pattern).
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("first_name", "last_name")
    )
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("date_of_birth")
    )
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on("postcode")
    )
    return linker


def _assign_master_person_ids(clusters_df: pd.DataFrame) -> pd.DataFrame:
    """Map Splink's internal `cluster_id` to a stable, human-friendly
    `master_person_id` (e.g. "MP00001"), ordered by cluster_id for full
    reproducibility given a fixed seed."""
    distinct_clusters = sorted(clusters_df["cluster_id"].unique())
    id_map = {cid: f"MP{i + 1:05d}" for i, cid in enumerate(distinct_clusters)}
    clusters_df = clusters_df.copy()
    clusters_df["master_person_id"] = clusters_df["cluster_id"].map(id_map)
    return clusters_df


def _per_record_match_probability(
    clusters_df: pd.DataFrame, predictions_df: pd.DataFrame
) -> pd.Series:
    """Splink scores pairwise edges, not individual records, so we derive a
    per-record confidence as the strongest edge connecting that record to
    another member of its own cluster. Records in a singleton cluster (no
    duplicates found) have nothing to compare against, so they default to
    1.0 - there is no ambiguity about which person they are."""
    cluster_of = clusters_df.set_index(UNIQUE_ID_COL)["cluster_id"]

    within_cluster = predictions_df[
        cluster_of.reindex(predictions_df[f"{UNIQUE_ID_COL}_l"]).values
        == cluster_of.reindex(predictions_df[f"{UNIQUE_ID_COL}_r"]).values
    ]

    best_l = within_cluster.groupby(f"{UNIQUE_ID_COL}_l")["match_probability"].max()
    best_r = within_cluster.groupby(f"{UNIQUE_ID_COL}_r")["match_probability"].max()
    best = pd.concat([best_l, best_r]).groupby(level=0).max()

    return clusters_df[UNIQUE_ID_COL].map(best).fillna(1.0)


@dataclass
class LinkageResult:
    clusters: int
    duplicates_found: int
    avg_match_probability: float
    training_seconds: float


def run_full_pipeline() -> LinkageResult:
    """Run the end-to-end Splink pipeline and persist results to DuckDB.

    Writes two tables:
      * `clusters`: source_record_id, master_person_id, match_probability
        (exactly the columns requested in the brief).
      * `person_cluster_map`: ground-truth person_index -> master_person_id,
        derived by majority vote across that person's records. This is
        used only to attach banking products (which are generated against
        the bank's own clean customer index) to the Splink-resolved
        cluster - see `wealth_service.py`.
    """
    start = time.perf_counter()
    conn = get_connection()
    try:
        source_df = _load_source_records(conn)
        if source_df.empty:
            raise RuntimeError("No source records found - call /generate-data first.")

        linker = _build_and_train_linker(source_df)

        predictions = linker.inference.predict(threshold_match_probability=PREDICT_MIN_THRESHOLD)
        predictions_df = predictions.as_pandas_dataframe()

        clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(
            predictions, threshold_match_probability=CLUSTER_MATCH_THRESHOLD
        )
        clusters_df = clusters.as_pandas_dataframe()[["cluster_id", UNIQUE_ID_COL]]

        clusters_df = _assign_master_person_ids(clusters_df)
        clusters_df["match_probability"] = _per_record_match_probability(clusters_df, predictions_df)

        result_df = clusters_df[[UNIQUE_ID_COL, "master_person_id", "match_probability"]]

        # Majority-vote mapping from ground-truth person_index to the
        # resolved cluster, used to attach banking products.
        person_index_lookup = source_df.set_index(UNIQUE_ID_COL)["person_index"]
        vote_df = result_df.copy()
        vote_df["person_index"] = vote_df[UNIQUE_ID_COL].map(person_index_lookup)
        person_cluster_map = (
            vote_df.groupby("person_index")["master_person_id"]
            .agg(lambda s: s.value_counts().idxmax())
            .reset_index()
        )

        _persist(conn, "clusters", result_df)
        _persist(conn, "person_cluster_map", person_cluster_map)

        n_records = len(source_df)
        n_clusters = result_df["master_person_id"].nunique()
        avg_prob = float(result_df["match_probability"].mean())
    finally:
        conn.close()

    return LinkageResult(
        clusters=n_clusters,
        duplicates_found=n_records - n_clusters,
        avg_match_probability=round(avg_prob, 6),
        training_seconds=round(time.perf_counter() - start, 2),
    )


def _persist(conn: duckdb.DuckDBPyConnection, table_name: str, df: pd.DataFrame) -> None:
    view_name = f"_{table_name}_incoming"
    conn.register(view_name, df)
    conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM {view_name}")
    conn.unregister(view_name)


def has_run_linkage() -> bool:
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        return "clusters" in tables
    finally:
        conn.close()
