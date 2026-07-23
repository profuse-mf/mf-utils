"""Analyse eligible applications vs successful lender redirections.

UTM lenders (lender_type=2):
  A = application_bre_logs in ClickHouse with empty criteria_missed, last 21 days
  B = mf_lender_rediections_stats in MySQL for that lender

API lenders (lender_type=1):
  A = lead_master in MySQL with status=1 for that lender
  B = mf_lender_rediections_stats in MySQL for that lender

Successful redirections = |A ∩ B|
Success % = |A ∩ B| / |A| * 100
"""

import sys

import clickhouse_connect
import pymysql

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    db_config,
)

MYSQL_CONFIG = db_config()
LOOKBACK_DAYS = 21
LENDER_TYPE_API = 1
LENDER_TYPE_UTM = 2


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
            SELECT id, lender_name, lender_type
            FROM mf_lenders
            ORDER BY id
            """
        )
        return cursor.fetchall()


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
    """Eligible API apps: lead_master.status = 1 for this lender."""
    with mysql_conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT application_id
            FROM lead_master
            WHERE lender_id = %s
              AND status = 1
              AND application_id IS NOT NULL
              AND application_id != 0
            """,
            (lender_id,),
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


def print_table(rows):
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

    widths = [
        max(len(r[i]) for r in table_rows) for i in range(len(headers))
    ]

    def fmt(row):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    separator = "-+-".join("-" * w for w in widths)
    print(fmt(table_rows[0]))
    print(separator)
    for row in table_rows[1:]:
        print(fmt(row))


def analyse_lender_redirections():
    mysql_conn = pymysql.connect(**MYSQL_CONFIG)
    ch_client = get_clickhouse_client()
    rows = []

    try:
        lenders = fetch_lenders(mysql_conn)
        print(f"Loaded {len(lenders)} lender(s) from mf_lenders")
        print(
            f"UTM eligibility window: last {LOOKBACK_DAYS} days "
            f"(application_bre_logs.created)"
        )
        print("API eligibility: lead_master.status = 1")
        print()

        for lender in lenders:
            lender_id = lender["id"]
            lender_name = lender["lender_name"] or "Unknown"
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
        return rows
    finally:
        mysql_conn.close()


if __name__ == "__main__":
    try:
        analyse_lender_redirections()
    except Exception as exc:
        print(f"Lender redirect analysis failed: {exc}", file=sys.stderr)
        sys.exit(1)
