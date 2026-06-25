"""Server-rendered exports for the Single View of Wealth (SVW) platform.

Pure rendering layer - every function here takes data already produced by
`wealth_service` and turns it into a downloadable file. No new DuckDB
queries beyond calling the existing `wealth_service`/`search_person` lookups
with a much larger limit than their on-screen-widget defaults, so an export
never silently truncates the dataset it claims to cover.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import wealth_service

# "Export everything" caps - large enough to cover this demo's ~10k profiles
# without a request needing to specify it explicitly.
DIRECTORY_EXPORT_LIMIT = 100_000
DEFAULT_REVIEW_QUEUE_EXPORT_LIMIT = 500


def build_directory_csv(q: str = "") -> str:
    """CSV of every resolved profile matching `q` (or all profiles, if
    `q` is blank) - mirrors what `/search` shows, without its `limit=50`
    on-screen default."""
    rows, _total = wealth_service.search_person(q, limit=DIRECTORY_EXPORT_LIMIT)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "master_person_id",
            "name",
            "linked_subsidiaries",
            "record_count",
            "match_probability",
            "salary",
            "cash",
            "savings",
            "investments",
            "mortgage",
            "net_wealth",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["master_person_id"],
                row["name"],
                "|".join(row["subsidiaries"]),
                row["record_count"],
                round(float(row["match_probability"]), 6),
                row["salary"],
                row["cash"],
                row["savings"],
                row["investments"],
                row["mortgage"],
                row["net_wealth"],
            ]
        )
    return buffer.getvalue()


def build_review_queue_csv(limit: int = DEFAULT_REVIEW_QUEUE_EXPORT_LIMIT) -> str:
    """CSV of the lowest-confidence multi-record clusters, worst first -
    the full manual-review backlog rather than the dashboard's top-10."""
    metrics = wealth_service.get_quality_metrics(review_queue_size=limit)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["master_person_id", "name", "match_probability", "record_count", "linked_subsidiaries"])
    for item in metrics["review_queue"]:
        writer.writerow(
            [
                item["master_person_id"],
                item["name"],
                item["match_probability"],
                item["record_count"],
                "|".join(item["linked_subsidiaries"]),
            ]
        )
    return buffer.getvalue()


_PDF_STYLES = getSampleStyleSheet()


def build_profile_pdf(profile: dict) -> bytes:
    """Render one resolved profile's full dossier as a PDF - same dict
    shape `wealth_service.get_profile_detail()` returns (and that backs the
    Profile page / `/wealth/{id}/detail` JSON response)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    story = []

    story.append(Paragraph("Single View of Wealth - Profile Report", _PDF_STYLES["Title"]))
    story.append(
        Paragraph(
            f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            _PDF_STYLES["Normal"],
        )
    )
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(f"{profile['name']} ({profile['master_person_id']})", _PDF_STYLES["Heading2"]))
    story.append(
        Paragraph(
            f"Wealth tier: {profile['wealth_tier']} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"Wealth percentile: {profile['wealth_score']} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"Linkage confidence: {profile['match_probability']:.1%}",
            _PDF_STYLES["Normal"],
        )
    )
    story.append(Spacer(1, 6 * mm))

    kpi_table = Table(
        [
            ["Salary", "Cash", "Savings", "Investments", "Mortgage", "Net wealth"],
            [
                f"£{profile['salary']:,.0f}",
                f"£{profile['cash']:,.0f}",
                f"£{profile['savings']:,.0f}",
                f"£{profile['investments']:,.0f}",
                f"£{profile['mortgage']:,.0f}",
                f"£{profile['net_wealth']:,.0f}",
            ],
        ],
        hAlign="LEFT",
    )
    kpi_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c2e30")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    story.append(kpi_table)
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("Linked subsidiary records", _PDF_STYLES["Heading3"]))
    record_rows = [["Subsidiary", "Employee ID", "Name", "Salary", "Confidence"]]
    for r in profile["linked_records"]:
        record_rows.append(
            [
                r["subsidiary"],
                r["employee_id"],
                f"{r['first_name']} {r['last_name']}",
                f"£{r['annual_salary']:,.0f}",
                f"{r['match_probability']:.1%}",
            ]
        )
    record_table = Table(record_rows, hAlign="LEFT")
    record_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c2e30")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ]
        )
    )
    story.append(record_table)
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("Banking products by subsidiary", _PDF_STYLES["Heading3"]))
    if profile["product_holdings"]:
        holding_rows = [["Product", "Subsidiary", "Account ID", "Balance", "Confidence"]]
        for h in profile["product_holdings"]:
            holding_rows.append(
                [
                    h["product_type"].replace("_", " ").title(),
                    h["subsidiary"],
                    h["account_id"],
                    f"£{h['balance']:,.0f}",
                    f"{h['match_probability']:.1%}",
                ]
            )
        holding_table = Table(holding_rows, hAlign="LEFT")
        holding_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c2e30")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ]
            )
        )
        story.append(holding_table)
    else:
        story.append(Paragraph("No banking products on file.", _PDF_STYLES["Normal"]))
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph("Field-level match explanation", _PDF_STYLES["Heading3"]))
    agreement_rows = [["Field", "Consistent across records?", "Distinct values seen"]]
    for fa in profile["field_agreement"]:
        agreement_rows.append(
            [fa["field"], "Yes" if fa["is_consistent"] else "No", ", ".join(fa["distinct_values"])]
        )
    agreement_table = Table(agreement_rows, hAlign="LEFT", colWidths=[35 * mm, 45 * mm, 90 * mm])
    agreement_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c2e30")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ]
        )
    )
    story.append(agreement_table)

    doc.build(story)
    return buffer.getvalue()
