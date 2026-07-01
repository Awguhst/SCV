"""Wealth aggregation: turns Splink clusters + the unified `records` table
into the bank's "golden" Single View of Wealth per resolved individual.

Aggregation choices worth calling out:

* **salary** is the *average* annual_salary across a cluster's linked
  payroll records (`record_type = 'PAYROLL'`), not the sum. Duplicate
  records represent the same underlying employment captured slightly
  differently by each subsidiary's payroll feed (rounding, timing, minor
  re-entry); summing would double-count one job, while min/max would
  arbitrarily discard a data point. Averaging reconciles the small,
  expected discrepancies between feeds.
* **cash / savings / investments / mortgage** are *summed* across all of
  a person's accounts of that type, since someone can genuinely hold
  multiple current accounts, ISAs, etc., and each one contributes real
  wealth/liability.
* Banking products are no longer a clean, ground-truth attachment. Each
  product account (a `records` row with a non-`'PAYROLL'` `record_type`)
  carries the same kind of noisy, independently-captured identity fields
  payroll records do, and flows through the *same* Splink dedupe pool (see
  `splink_service.py`). A product attaches to a `master_person_id` exactly
  the way a payroll record does - by being a member of that cluster in the
  `clusters` table - not via any ground-truth shortcut. This means product
  attachment is now subject to genuine linkage error (a product could in
  principle be merged into the wrong person's cluster, or fail to merge
  into the right one) the same way a noisy payroll record always has
  been. Each product account is still tagged with the subsidiary it sits
  at, and a person may hold several accounts of the same type across
  different subsidiaries (e.g. savings at one subsidiary, a mortgage at
  another).
"""

from __future__ import annotations

from collections import Counter

from app.data_generator import get_connection

# net_wealth thresholds used to label a profile's segment on the detail page -
# a simplified version of the mass-market / affluent / HNW / UHNW tiers banks
# commonly use for proposition targeting.
_WEALTH_TIERS = (
    (0, "Negative Equity"),
    (100_000, "Mass Market"),
    (500_000, "Affluent"),
    (2_000_000, "High Net Worth"),
)
_WEALTH_TIER_TOP = "Ultra High Net Worth"


def _wealth_tier(net_wealth: float) -> str:
    for ceiling, label in _WEALTH_TIERS:
        if net_wealth < ceiling:
            return label
    return _WEALTH_TIER_TOP

_WEALTH_PROFILES_SQL = """
CREATE OR REPLACE TABLE wealth_profiles AS
WITH name_counts AS (
    -- Drawn from every record in the cluster regardless of record_type -
    -- payroll and banking-product records are equally first-class, noisy
    -- identity captures now, all living in one `records` table. This also
    -- guarantees a cluster gets a display name even in the (rare, linkage-
    -- error) case where every payroll record ends up split into a
    -- different cluster than this person's product records.
    SELECT
        c.master_person_id,
        r.first_name,
        r.last_name,
        COUNT(*) AS occurrences
    FROM clusters c
    JOIN records r USING (source_record_id)
    GROUP BY 1, 2, 3
),
ranked_names AS (
    -- Pick the "best" of the noisy name variants linked into each cluster for
    -- display: prefer a fuller name over an initial (e.g. "Billy" over "B"),
    -- then proper-case spelling over ALL CAPS / all lowercase, then whichever
    -- variant was recorded most often.
    SELECT
        master_person_id,
        first_name,
        last_name,
        ROW_NUMBER() OVER (
            PARTITION BY master_person_id
            ORDER BY
                LENGTH(first_name) DESC,
                (first_name != UPPER(first_name) AND first_name != LOWER(first_name)) DESC,
                (last_name != UPPER(last_name) AND last_name != LOWER(last_name)) DESC,
                occurrences DESC
        ) AS rn
    FROM name_counts
),
best_name AS (
    SELECT master_person_id, first_name || ' ' || last_name AS name
    FROM ranked_names
    WHERE rn = 1
),
salary_agg AS (
    SELECT c.master_person_id, AVG(r.annual_salary) AS salary
    FROM clusters c
    JOIN records r USING (source_record_id)
    WHERE r.record_type = 'PAYROLL'
    GROUP BY 1
),
cash_agg AS (
    SELECT c.master_person_id, SUM(r.balance) AS cash
    FROM clusters c
    JOIN records r USING (source_record_id)
    WHERE r.record_type = 'CURRENT_ACCOUNT'
    GROUP BY 1
),
savings_agg AS (
    SELECT c.master_person_id, SUM(r.balance) AS savings
    FROM clusters c
    JOIN records r USING (source_record_id)
    WHERE r.record_type = 'SAVINGS_ACCOUNT'
    GROUP BY 1
),
investments_agg AS (
    SELECT c.master_person_id, SUM(r.balance) AS investments
    FROM clusters c
    JOIN records r USING (source_record_id)
    WHERE r.record_type = 'INVESTMENT'
    GROUP BY 1
),
mortgage_agg AS (
    SELECT c.master_person_id, SUM(r.balance) AS mortgage
    FROM clusters c
    JOIN records r USING (source_record_id)
    WHERE r.record_type = 'MORTGAGE'
    GROUP BY 1
)
SELECT
    bn.master_person_id,
    bn.name,
    COALESCE(sal.salary, 0) AS salary,
    COALESCE(cash.cash, 0) AS cash,
    COALESCE(sav.savings, 0) AS savings,
    COALESCE(inv.investments, 0) AS investments,
    COALESCE(mtg.mortgage, 0) AS mortgage,
    COALESCE(cash.cash, 0) + COALESCE(sav.savings, 0) + COALESCE(inv.investments, 0)
        - COALESCE(mtg.mortgage, 0) AS net_wealth
FROM best_name bn
LEFT JOIN salary_agg sal USING (master_person_id)
LEFT JOIN cash_agg cash USING (master_person_id)
LEFT JOIN savings_agg sav USING (master_person_id)
LEFT JOIN investments_agg inv USING (master_person_id)
LEFT JOIN mortgage_agg mtg USING (master_person_id)
"""


def build_wealth_profiles() -> int:
    """(Re)build the `wealth_profiles` table from clusters + the unified
    `records` table.

    Returns the number of golden profiles created.
    """
    conn = get_connection()
    try:
        conn.execute(_WEALTH_PROFILES_SQL)
        return conn.execute("SELECT COUNT(*) FROM wealth_profiles").fetchone()[0]
    finally:
        conn.close()


def get_wealth_profile(master_person_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM wealth_profiles WHERE master_person_id = ?", [master_person_id]
        ).fetchone()
        if row is None:
            return None
        columns = [c[0] for c in conn.description]
        return dict(zip(columns, row))
    finally:
        conn.close()


_FIELD_AGREEMENT_COLUMNS = ("email", "phone", "address", "postcode", "date_of_birth", "last_name")


def get_profile_detail(master_person_id: str) -> dict | None:
    """The full profile dossier behind the "Profile" page: the golden wealth
    profile plus every linked record (payroll and banking-product alike,
    via the unified `records` table) and a field-by-field agreement
    breakdown explaining *why* Splink linked these records (and flagging
    anywhere the underlying records still disagree)."""
    conn = get_connection()
    try:
        profile_row = conn.execute(
            "SELECT * FROM wealth_profiles WHERE master_person_id = ?", [master_person_id]
        ).fetchone()
        if profile_row is None:
            return None
        profile = dict(zip([c[0] for c in conn.description], profile_row))
        profile["wealth_tier"] = _wealth_tier(profile["net_wealth"])

        wealth_score_row = conn.execute(
            """
            SELECT wealth_score FROM (
                SELECT master_person_id, PERCENT_RANK() OVER (ORDER BY net_wealth) * 100 AS wealth_score
                FROM wealth_profiles
            )
            WHERE master_person_id = ?
            """,
            [master_person_id],
        ).fetchone()
        profile["wealth_score"] = round(float(wealth_score_row[0]), 1) if wealth_score_row else 0.0

        record_rows = conn.execute(
            """
            SELECT
                r.source_record_id, r.subsidiary, r.record_type, r.employee_id, r.account_id,
                r.first_name, r.last_name, r.date_of_birth, r.email, r.phone, r.address, r.city, r.postcode,
                r.annual_salary, r.bonus, r.balance, r.currency, c.match_probability
            FROM clusters c
            JOIN records r USING (source_record_id)
            WHERE c.master_person_id = ?
            ORDER BY r.record_type, r.subsidiary
            """,
            [master_person_id],
        ).fetchall()
        record_cols = [c[0] for c in conn.description]
        all_records = [dict(zip(record_cols, row)) for row in record_rows]
        profile["records"] = all_records

        # record_count/linked_subsidiaries/primary_city/primary_postcode keep
        # their historical, payroll-specific meaning: the generator caps
        # payroll records at 1-4 per person, while banking products have no
        # such cap, so counting both together here would conflate two
        # different things.
        payroll_records = [r for r in all_records if r["record_type"] == "PAYROLL"]
        profile["record_count"] = len(payroll_records)
        profile["linked_subsidiaries"] = sorted({r["subsidiary"] for r in payroll_records})

        # Usually averaged just over linked payroll records, but a cluster can
        # (rare linkage-split edge case) end up with zero payroll members and
        # only banking-product ones - averaging over every record in the
        # cluster means this never divides by zero, consistent with
        # name_counts treating both record kinds as equally first-class
        # evidence for this cluster.
        all_confidences = [r["match_probability"] for r in all_records]
        profile["match_probability"] = round(sum(all_confidences) / len(all_confidences), 6)

        cities = [r["city"] for r in payroll_records if r["city"]]
        postcodes = [r["postcode"] for r in payroll_records if r["postcode"]]
        profile["primary_city"] = Counter(cities).most_common(1)[0][0] if cities else None
        profile["primary_postcode"] = Counter(postcodes).most_common(1)[0][0] if postcodes else None

        # Field agreement spans every linked record - payroll and banking
        # product alike - since both carry the same noisy, independently-
        # captured identity fields. Restricting this to payroll only would
        # ignore real evidence and would trivially mark every field
        # "consistent" for the rare all-product, zero-payroll cluster.
        profile["field_agreement"] = [
            {
                "field": field,
                "is_consistent": len({r[field] for r in all_records if r[field]}) <= 1,
                "distinct_values": sorted({r[field] for r in all_records if r[field]}),
            }
            for field in _FIELD_AGREEMENT_COLUMNS
        ]

        return profile
    finally:
        conn.close()


def get_showcase_example() -> dict | None:
    """Pick one resolved cluster that makes a good "before/after Splink"
    showcase for the dashboard: 3-4 linked payroll records (the generator
    never gives one ground-truth person more than 4 - a bigger cluster than
    that is itself a false-merge linkage error, not a clean example to
    showcase) with at least 2 different first-name spellings/variants among
    them, so the "before" side visibly looks like unrelated people. Among
    qualifying candidates, one that also holds at least one banking product
    is preferred (so the showcase can tell the product story too), but this
    is only a soft preference - it never overrides the hard requirements
    above. Picked from the live `clusters`/`records` tables only - no
    ground truth involved, so this works exactly the same way it would in
    production.

    Returns the same shape as `get_profile_detail`, or None if no cluster
    in the current dataset meets that bar (falls back to the single
    largest cluster, or None if there are no clusters at all).
    """
    conn = get_connection()
    try:
        candidate = conn.execute(
            """
            SELECT c.master_person_id
            FROM clusters c
            JOIN records s USING (source_record_id)
            WHERE s.record_type = 'PAYROLL'
            GROUP BY c.master_person_id
            HAVING COUNT(*) BETWEEN 3 AND 4 AND COUNT(DISTINCT LOWER(s.first_name)) >= 2
            ORDER BY
                EXISTS (
                    SELECT 1 FROM clusters pc
                    JOIN records pr USING (source_record_id)
                    WHERE pc.master_person_id = c.master_person_id AND pr.record_type != 'PAYROLL'
                ) DESC,
                COUNT(*) DESC,
                c.master_person_id
            LIMIT 1
            """
        ).fetchone()
        if candidate is None:
            candidate = conn.execute(
                """
                SELECT master_person_id
                FROM clusters
                GROUP BY master_person_id
                ORDER BY COUNT(*) DESC, master_person_id
                LIMIT 1
                """
            ).fetchone()
        if candidate is None:
            return None
        master_person_id = candidate[0]
    finally:
        conn.close()

    return get_profile_detail(master_person_id)


_CLUSTER_STATS_CTE = """
    cluster_stats AS (
        -- LEFT JOIN (not INNER), with the record_type filter in the join
        -- condition rather than a WHERE clause: a cluster can consist
        -- entirely of banking-product records with zero payroll members (a
        -- rare linkage-split edge case) - subsidiaries/record_count stay
        -- scoped to payroll records specifically (their documented meaning
        -- elsewhere), but every cluster must still get a row here, or it
        -- would be silently invisible in search/the Directory despite
        -- correctly appearing in wealth_profiles and the dashboard totals.
        -- Putting the record_type filter in a WHERE after the join would
        -- silently turn this back into an inner join for those clusters.
        SELECT
            c.master_person_id,
            COALESCE(LIST(DISTINCT s.subsidiary) FILTER (WHERE s.subsidiary IS NOT NULL), []) AS subsidiaries,
            COUNT(s.source_record_id) AS record_count,
            AVG(c.match_probability) AS match_probability
        FROM clusters c
        LEFT JOIN records s ON s.source_record_id = c.source_record_id AND s.record_type = 'PAYROLL'
        GROUP BY 1
    )
"""


def search_person(query: str, limit: int = 50) -> tuple[list[dict], int]:
    """Search resolved profiles by name, matching either the chosen display
    name or any of the underlying linked records - payroll or banking
    product alike, since both carry the same noisy identity fields - so a
    search for "Jonathan Smith" still finds a cluster whose display name
    resolved to the more common "John Smith" variant, even if that spelling
    only appears on one of the person's banking-product holdings.

    An empty/blank query skips filtering entirely and instead browses *all*
    profiles alphabetically by name - this is what powers the Directory
    view's default listing before the user has typed anything.

    Returns (results, total) - total is the full match count, which may
    exceed len(results) once `limit` kicks in.
    """
    query = query.strip()
    conn = get_connection()
    try:
        if query:
            like_query = f"%{query}%"
            total = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT c.master_person_id
                    FROM clusters c
                    JOIN records s USING (source_record_id)
                    WHERE (s.first_name || ' ' || s.last_name) ILIKE ?
                    UNION
                    SELECT master_person_id FROM wealth_profiles WHERE name ILIKE ?
                )
                """,
                [like_query, like_query],
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                WITH matched_clusters AS (
                    SELECT DISTINCT c.master_person_id
                    FROM clusters c
                    JOIN records s USING (source_record_id)
                    WHERE (s.first_name || ' ' || s.last_name) ILIKE ?
                    UNION
                    SELECT master_person_id FROM wealth_profiles WHERE name ILIKE ?
                ),
                {_CLUSTER_STATS_CTE}
                SELECT wp.*, cs.subsidiaries, cs.record_count, cs.match_probability
                FROM wealth_profiles wp
                JOIN matched_clusters mc USING (master_person_id)
                JOIN cluster_stats cs USING (master_person_id)
                ORDER BY wp.net_wealth DESC
                LIMIT ?
                """,
                [like_query, like_query, limit],
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM wealth_profiles").fetchone()[0]
            rows = conn.execute(
                f"""
                WITH {_CLUSTER_STATS_CTE}
                SELECT wp.*, cs.subsidiaries, cs.record_count, cs.match_probability
                FROM wealth_profiles wp
                JOIN cluster_stats cs USING (master_person_id)
                ORDER BY wp.name ASC
                LIMIT ?
                """,
                [limit],
            ).fetchall()

        columns = [c[0] for c in conn.description]
        return [dict(zip(columns, row)) for row in rows], total
    finally:
        conn.close()


def get_dashboard_summary() -> dict:
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}

        unique_people = (
            conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] if "persons" in tables else 0
        )
        source_records = (
            conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if "records" in tables
            else 0
        )

        clusters = 0
        total_linked_records = 0
        avg_match_probability = 0.0
        if "clusters" in tables:
            clusters = conn.execute("SELECT COUNT(DISTINCT master_person_id) FROM clusters").fetchone()[0]
            total_linked_records = conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
            avg_row = conn.execute("SELECT AVG(match_probability) FROM clusters").fetchone()[0]
            avg_match_probability = round(float(avg_row), 6) if avg_row is not None else 0.0

        total_assets = 0.0
        total_cash = total_savings = total_investments = total_mortgage = total_net_wealth = 0.0
        if "wealth_profiles" in tables:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(cash + savings + investments), 0),
                    COALESCE(SUM(cash), 0),
                    COALESCE(SUM(savings), 0),
                    COALESCE(SUM(investments), 0),
                    COALESCE(SUM(mortgage), 0),
                    COALESCE(SUM(net_wealth), 0)
                FROM wealth_profiles
                """
            ).fetchone()
            (
                total_assets,
                total_cash,
                total_savings,
                total_investments,
                total_mortgage,
                total_net_wealth,
            ) = (float(x) for x in row)

        subsidiary_record_counts: dict[str, int] = {}
        if "records" in tables:
            subsidiary_record_counts = dict(
                conn.execute(
                    "SELECT subsidiary, COUNT(*) FROM records WHERE record_type = 'PAYROLL' GROUP BY 1 ORDER BY 1"
                ).fetchall()
            )

        product_subsidiary_counts: dict[str, int] = {}
        if "records" in tables:
            product_subsidiary_counts = dict(
                conn.execute(
                    "SELECT subsidiary, COUNT(*) FROM records WHERE record_type != 'PAYROLL' GROUP BY 1 ORDER BY 1"
                ).fetchall()
            )

        return {
            "unique_people": unique_people,
            "source_records": source_records,
            "clusters": clusters,
            "avg_match_probability": avg_match_probability,
            "total_assets": round(total_assets, 2),
            "duplicates_found": max(total_linked_records - clusters, 0),
            "total_cash": round(total_cash, 2),
            "total_savings": round(total_savings, 2),
            "total_investments": round(total_investments, 2),
            "total_mortgage": round(total_mortgage, 2),
            "total_net_wealth": round(total_net_wealth, 2),
            "subsidiary_record_counts": subsidiary_record_counts,
            "product_subsidiary_counts": product_subsidiary_counts,
        }
    finally:
        conn.close()


def get_quality_metrics(review_queue_size: int = 10) -> dict:
    """Linkage-quality metrics for the Data Quality dashboard page: how
    confident the model was across all clusters, how big clusters typically
    are, and which clusters are least confident (candidates for manual
    analyst review)."""
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        if "clusters" not in tables:
            return {"match_probability_histogram": [], "cluster_size_distribution": [], "review_queue": []}

        histogram_rows = conn.execute(
            """
            SELECT
                CASE
                    WHEN match_probability >= 0.99 THEN '>= 0.99'
                    WHEN match_probability >= 0.95 THEN '0.95 - 0.99'
                    WHEN match_probability >= 0.90 THEN '0.90 - 0.95'
                    WHEN match_probability >= 0.75 THEN '0.75 - 0.90'
                    ELSE '< 0.75'
                END AS bucket,
                COUNT(*) AS n
            FROM clusters
            GROUP BY 1
            """
        ).fetchall()
        bucket_order = ['< 0.75', '0.75 - 0.90', '0.90 - 0.95', '0.95 - 0.99', '>= 0.99']
        histogram_counts = dict(histogram_rows)
        match_probability_histogram = [
            {"label": label, "count": histogram_counts.get(label, 0)} for label in bucket_order
        ]

        size_rows = conn.execute(
            """
            WITH sizes AS (
                SELECT master_person_id, COUNT(*) AS n FROM clusters GROUP BY 1
            )
            SELECT CASE WHEN n >= 4 THEN '4+' ELSE CAST(n AS VARCHAR) END AS bucket, COUNT(*) AS clusters
            FROM sizes
            GROUP BY 1
            """
        ).fetchall()
        size_order = ['1', '2', '3', '4+']
        size_counts = dict(size_rows)
        cluster_size_distribution = [
            {"label": label, "count": size_counts.get(label, 0)} for label in size_order
        ]

        review_queue_rows = conn.execute(
            """
            SELECT wp.master_person_id, wp.name, cs.match_probability, cs.record_count, cs.subsidiaries
            FROM wealth_profiles wp
            JOIN (
                SELECT
                    c.master_person_id,
                    AVG(c.match_probability) AS match_probability,
                    COUNT(*) AS record_count,
                    LIST(DISTINCT s.subsidiary) AS subsidiaries
                FROM clusters c
                JOIN records s ON s.source_record_id = c.source_record_id AND s.record_type = 'PAYROLL'
                GROUP BY 1
            ) cs USING (master_person_id)
            WHERE cs.record_count > 1
            ORDER BY cs.match_probability ASC
            LIMIT ?
            """,
            [review_queue_size],
        ).fetchall()
        review_queue = [
            {
                "master_person_id": r[0],
                "name": r[1],
                "match_probability": round(float(r[2]), 6),
                "record_count": r[3],
                "linked_subsidiaries": list(r[4]),
            }
            for r in review_queue_rows
        ]

        return {
            "match_probability_histogram": match_probability_histogram,
            "cluster_size_distribution": cluster_size_distribution,
            "review_queue": review_queue,
        }
    finally:
        conn.close()


def has_wealth_profiles() -> bool:
    conn = get_connection()
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        return "wealth_profiles" in tables
    finally:
        conn.close()
