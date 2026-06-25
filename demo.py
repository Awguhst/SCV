"""Standalone "before/after Splink" demo for the Single View of Wealth platform.

Generates the synthetic dataset, picks one person who has data-quality
issues across at least three subsidiary records, shows how those records
look like unrelated people *before* linkage, runs the Splink pipeline,
then shows the resolved master_person_id and the resulting wealth view.

Run from the project root with:
    python demo.py
(or, on Windows, using the bundled conda env: .\\env\\python.exe demo.py)
"""

from __future__ import annotations

import sys

from app import data_generator, splink_service, wealth_service
from app.data_generator import get_connection

# Make sure the £ symbol renders correctly on Windows consoles that default
# to a legacy (non-UTF-8) code page.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    _print_header("STEP 1: Generating synthetic multi-subsidiary payroll data")
    result = data_generator.generate_all()
    print(f"Generated {result.people:,} unique people across {result.records:,} source records.")

    conn = get_connection()
    try:
        # Note: a person can now hold more than one account of the same
        # product type (each at a possibly different subsidiary), so the
        # "has every product type" check below uses EXISTS rather than an
        # INNER JOIN on product_records - joining directly would create a
        # Cartesian product and inflate COUNT(*) with nothing to do with
        # the number of subsidiary payroll records, which is what this
        # query actually needs to count.
        demo_person_index = conn.execute(
            """
            SELECT s.person_index
            FROM source_records s
            WHERE EXISTS (SELECT 1 FROM product_records p WHERE p.person_index = s.person_index AND p.record_type = 'current_account')
              AND EXISTS (SELECT 1 FROM product_records p WHERE p.person_index = s.person_index AND p.record_type = 'savings_account')
              AND EXISTS (SELECT 1 FROM product_records p WHERE p.person_index = s.person_index AND p.record_type = 'investment')
              AND EXISTS (SELECT 1 FROM product_records p WHERE p.person_index = s.person_index AND p.record_type = 'mortgage')
            GROUP BY s.person_index
            HAVING COUNT(*) >= 3 AND COUNT(DISTINCT LOWER(s.first_name)) >= 2
            ORDER BY s.person_index
            LIMIT 1
            """
        ).fetchone()[0]

        _print_header("STEP 2: BEFORE Splink - records look like different people")
        before_payroll_rows = conn.execute(
            """
            SELECT subsidiary, employee_id, first_name, last_name, email, phone, address, postcode, annual_salary
            FROM source_records
            WHERE person_index = ?
            ORDER BY subsidiary
            """,
            [demo_person_index],
        ).fetchall()
        payroll_columns = [c[0] for c in conn.description]
        for row in before_payroll_rows:
            rec = dict(zip(payroll_columns, row))
            print(
                f"- [{rec['subsidiary']}] {rec['first_name']} {rec['last_name']}  payroll, salary=£{rec['annual_salary']:,.0f}  "
                f"email={rec['email']}  phone={rec['phone']}  "
                f"address={rec['address']}, {rec['postcode']}"
            )

        before_product_rows = conn.execute(
            """
            SELECT subsidiary, record_type, account_id, first_name, last_name, email, phone, address, postcode, balance
            FROM product_records
            WHERE person_index = ?
            ORDER BY subsidiary
            """,
            [demo_person_index],
        ).fetchall()
        product_columns = [c[0] for c in conn.description]
        for row in before_product_rows:
            rec = dict(zip(product_columns, row))
            print(
                f"- [{rec['subsidiary']}] {rec['first_name']} {rec['last_name']}  {rec['record_type']}, balance=£{rec['balance']:,.0f}  "
                f"email={rec['email']}  phone={rec['phone']}  "
                f"address={rec['address']}, {rec['postcode']}"
            )

        total_before = len(before_payroll_rows) + len(before_product_rows)
        print(f"\n=> Without entity resolution, these look like {total_before} different people.")
    finally:
        conn.close()

    _print_header("STEP 3: Running Splink entity resolution")
    linkage = splink_service.run_full_pipeline()
    wealth_service.build_wealth_profiles()
    print(f"Clusters resolved:    {linkage.clusters:,}")
    print(f"Duplicates found:     {linkage.duplicates_found:,}")
    print(f"Avg match confidence: {linkage.avg_match_probability:.1%}")

    conn = get_connection()
    try:
        master_person_id = conn.execute(
            """
            SELECT c.master_person_id
            FROM source_records s
            JOIN clusters c USING (source_record_id)
            WHERE s.person_index = ?
            LIMIT 1
            """,
            [demo_person_index],
        ).fetchone()[0]

        _print_header(f"STEP 4: AFTER Splink - Master Person ID: {master_person_id}")
        after_payroll_rows = conn.execute(
            """
            SELECT s.subsidiary, c.match_probability
            FROM clusters c
            JOIN source_records s USING (source_record_id)
            WHERE c.master_person_id = ?
            ORDER BY s.subsidiary
            """,
            [master_person_id],
        ).fetchall()
        for subsidiary, prob in after_payroll_rows:
            print(f"- Linked: {subsidiary} (payroll)  (confidence: {prob:.1%})")

        after_product_rows = conn.execute(
            """
            SELECT p.subsidiary, p.record_type, c.match_probability
            FROM clusters c
            JOIN product_records p USING (source_record_id)
            WHERE c.master_person_id = ?
            ORDER BY p.subsidiary
            """,
            [master_person_id],
        ).fetchall()
        for subsidiary, record_type, prob in after_product_rows:
            print(f"- Linked: {subsidiary} ({record_type})  (confidence: {prob:.1%})")

        all_confidences = [p for _, p in after_payroll_rows] + [p for _, _, p in after_product_rows]
        avg_conf = sum(all_confidences) / len(all_confidences)
        print(f"\n=> Resolved {len(all_confidences)} records into ONE person. Overall confidence: {avg_conf:.1%}")

        _print_header("STEP 5: Single View of Wealth")
        profile = wealth_service.get_wealth_profile(master_person_id)
        print(f"Name:          {profile['name']}")
        print(f"Salary:        £{profile['salary']:,.0f}")
        print(f"Current Acc.:  £{profile['cash']:,.0f}")
        print(f"Savings:       £{profile['savings']:,.0f}")
        print(f"Investments:   £{profile['investments']:,.0f}")
        print(f"Mortgage:      £{profile['mortgage']:,.0f}")
        print(f"Net Wealth:    £{profile['net_wealth']:,.0f}")

        holdings = wealth_service.get_profile_detail(master_person_id)["product_holdings"]
        n_subsidiaries = len({h["subsidiary"] for h in holdings})
        print(f"\n{len(holdings)} banking-product account(s) across {n_subsidiaries} subsidiary(ies):")
        for h in holdings:
            print(
                f"- [{h['subsidiary']}] {h['product_type']}: £{h['balance']:,.0f}  "
                f"(confidence: {h['match_probability']:.1%})"
            )
    finally:
        conn.close()

    _print_header("STEP 6: Platform dashboard")
    summary = wealth_service.get_dashboard_summary()
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
