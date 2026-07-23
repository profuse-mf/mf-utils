"""Analyse eligible applications vs successful lender redirections.

UTM lenders (lender_type=2):
  A = application_bre_logs in ClickHouse with empty criteria_missed, last 21 days
  B = mf_lender_rediections_stats in MySQL for that lender

API lenders (lender_type=1):
  A = lead_master in MySQL with status=1 for that lender, last 21 days
  B = mf_lender_rediections_stats in MySQL for that lender

Successful redirections = |A ∩ B|
Success % = |A ∩ B| / |A| * 100
"""

import html
import smtplib
import sys
from datetime import date, datetime
from email.message import EmailMessage

import clickhouse_connect
import pymysql

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    db_config,
)

MYSQL_CONFIG = db_config()
LOOKBACK_DAYS = 21
LENDER_TYPE_API = 1
LENDER_TYPE_UTM = 2
REPORT_EMAIL_TO = ["anup.vaze@appkhichadi.com"]


def send_email(
    subject,
    body,
    to_emails,
    from_email,
    smtp_host,
    smtp_port,
    smtp_user,
    smtp_password,
    html_body=None,
):
    """Same SMTP flow as mis_new.py (without attachment)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def get_clickhouse_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )


def fetch_lenders(mysql_conn):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, lender_name, product_offering, lender_type
            FROM mf_lenders
            ORDER BY id
            """
        )
        return cursor.fetchall()


def format_lender_display_name(lender_name, product_offering):
    name = (lender_name or "Unknown").strip() or "Unknown"
    offering = (product_offering or "").strip()
    if offering:
        return f"{name} - {offering}"
    return name


def fetch_utm_eligible_application_ids(ch_client, lender_id):
    """Eligible UTM apps: empty criteria_missed BRE log within LOOKBACK_DAYS."""
    query = f"""
        SELECT DISTINCT application_id
        FROM application_bre_logs
        WHERE lender_id = {{lender_id:UInt64}}
          AND created >= now() - INTERVAL {LOOKBACK_DAYS} DAY
          AND replaceRegexpAll(trimBoth(ifNull(criteria_missed, '')), '\\s', '')
              IN ('{{}}', '[]', '')
    """
    result = ch_client.query(query, parameters={"lender_id": int(lender_id)})
    return {int(row[0]) for row in result.result_rows if row[0] is not None}


def fetch_api_eligible_application_ids(mysql_conn, lender_id):
    """Eligible API apps: lead_master.status = 1 within LOOKBACK_DAYS."""
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT application_id
            FROM lead_master
            WHERE lender_id = %s
              AND status = 1
              AND application_id IS NOT NULL
              AND application_id != 0
              AND created >= NOW() - INTERVAL %s DAY
            """,
            (lender_id, LOOKBACK_DAYS),
        )
        return {int(row["application_id"]) for row in cursor.fetchall()}


def fetch_redirected_application_ids(mysql_conn, lender_id):
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT application_id
            FROM mf_lender_rediections_stats
            WHERE lender_id = %s
              AND application_id IS NOT NULL
              AND application_id != 0
            """,
            (lender_id,),
        )
        return {int(row["application_id"]) for row in cursor.fetchall()}


def success_pct(redirected, eligible):
    if not eligible:
        return 0.0
    return round((redirected * 100.0) / eligible, 2)


def lender_type_label(lender_type):
    if lender_type == LENDER_TYPE_API:
        return "API"
    if lender_type == LENDER_TYPE_UTM:
        return "UTM"
    return str(lender_type)


def format_table_rows(rows):
    headers = (
        "Lender ID",
        "Lender Name",
        "Type",
        "Eligible",
        "Redirected",
        "Success %",
    )
    table_rows = [headers]
    for row in rows:
        table_rows.append(
            (
                str(row["lender_id"]),
                row["lender_name"],
                row["type"],
                str(row["eligible"]),
                str(row["redirected"]),
                f"{row['success_pct']:.2f}%",
            )
        )
    return table_rows


def print_table(rows):
    table_rows = format_table_rows(rows)
    widths = [
        max(len(r[i]) for r in table_rows) for i in range(len(table_rows[0]))
    ]

    def fmt(row):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    separator = "-+-".join("-" * w for w in widths)
    print(fmt(table_rows[0]))
    print(separator)
    for row in table_rows[1:]:
        print(fmt(row))


def build_text_report(rows):
    table_rows = format_table_rows(rows)
    lines = [
        f"Lender Redirect Analysis — last {LOOKBACK_DAYS} days",
        f"Report date: {date.today()}",
        "",
    ]
    widths = [
        max(len(r[i]) for r in table_rows) for i in range(len(table_rows[0]))
    ]
    for i, row in enumerate(table_rows):
        lines.append(
            " | ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        )
        if i == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def build_html_report(rows):
    table_rows = format_table_rows(rows)
    header_cells = "".join(
        f"<th>{html.escape(cell)}</th>" for cell in table_rows[0]
    )
    body_rows = []
    for row in table_rows[1:]:
        cells = "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")

    return f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #111827;">
    <h2>Lender Redirect Analysis</h2>
    <p>Last {LOOKBACK_DAYS} days | Report date: {date.today()}</p>
    <table border="1" cellpadding="8" cellspacing="0"
           style="border-collapse: collapse; border-color: #cbd5e1;">
      <thead style="background: #f59e0b;">
        <tr>{header_cells}</tr>
      </thead>
      <tbody>
        {"".join(body_rows)}
      </tbody>
    </table>
  </body>
</html>
""".strip()


def send_report_email(rows):
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_USER and SMTP_PASSWORD in .env "
            "(same as mis_new.py)"
        )

    print(
        f"Sending report email via {SMTP_HOST}:{SMTP_PORT} "
        f"as {SMTP_USER} → {', '.join(REPORT_EMAIL_TO)}"
    )
    send_email(
        subject=f"Lender Redirect Analysis - {datetime.now().date()}",
        body=build_text_report(rows),
        to_emails=REPORT_EMAIL_TO,
        from_email=SMTP_FROM,
        smtp_host=SMTP_HOST,
        smtp_port=SMTP_PORT,
        smtp_user=SMTP_USER,
        smtp_password=SMTP_PASSWORD,
        html_body=build_html_report(rows),
    )
    print(f"Report email sent to {', '.join(REPORT_EMAIL_TO)}")


def analyse_lender_redirections():
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    ch_client = get_clickhouse_client()
    rows = []

    try:
        lenders = fetch_lenders(mysql_conn)
        print(f"Loaded {len(lenders)} lender(s) from mf_lenders")
        print(
            f"Eligibility window: last {LOOKBACK_DAYS} days "
            f"(UTM: application_bre_logs.created, API: lead_master.created)"
        )
        print("API eligibility also requires lead_master.status = 1")
        print()

        for lender in lenders:
            lender_id = lender["id"]
            lender_name = format_lender_display_name(
                lender.get("lender_name"),
                lender.get("product_offering"),
            )
            lender_type = int(lender["lender_type"] or 0)

            if lender_type == LENDER_TYPE_UTM:
                eligible_ids = fetch_utm_eligible_application_ids(
                    ch_client, lender_id
                )
            elif lender_type == LENDER_TYPE_API:
                eligible_ids = fetch_api_eligible_application_ids(
                    mysql_conn, lender_id
                )
            else:
                print(
                    f"Skipping lender_id={lender_id} "
                    f"({lender_name}): unknown lender_type={lender_type}"
                )
                continue

            redirected_ids = fetch_redirected_application_ids(
                mysql_conn, lender_id
            )
            successful_ids = eligible_ids & redirected_ids

            eligible = len(eligible_ids)
            redirected = len(successful_ids)
            rows.append(
                {
                    "lender_id": lender_id,
                    "lender_name": lender_name,
                    "type": lender_type_label(lender_type),
                    "eligible": eligible,
                    "redirected": redirected,
                    "success_pct": success_pct(redirected, eligible),
                }
            )

            not_redirected = eligible - redirected
            print(
                f"Processed lender_id={lender_id} ({lender_name}, "
                f"{lender_type_label(lender_type)}): "
                f"eligible={eligible}, redirected={redirected}, "
                f"eligible_not_redirected={not_redirected}"
            )

        print()
        print_table(rows)
        send_report_email(rows)
        return rows
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        analyse_lender_redirections()
    except Exception as exc:
        print(f"Lender redirect analysis failed: {exc}", file=sys.stderr)
        sys.exit(1)
