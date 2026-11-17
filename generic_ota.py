"""Fetch eligible applications for lender 15 and optionally POST them to a Node API."""
import argparse
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
import os

import pymysql
import requests

from config import db_config

DEFAULT_START_DATE = "2026-11-06"
DEFAULT_END_DATE = "2026-11-16"
DEFAULT_LENDER_ID = 15
DEFAULT_API_URL = os.environ.get(
    "LENDER_API_URL", "https://api.moneyfatafat.com/api/generic-ota"
)
API_KEY = os.environ.get("GENERIC_OTA_API_KEY", "").strip()
API_TIMEOUT_SECONDS = 30


def get_connection():
    """Open a MySQL connection using the shared project configuration."""
    return pymysql.connect(**db_config())


def build_application_query(
    start_date,
    end_date,
    lender_id=None,
    eligible_only=False,
    limit=None,
):
    """Build the parameterized query and values for the application fetch."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("end_date must not be before start_date")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    start_at = datetime.combine(start, time.min)
    end_before = datetime.combine(end + timedelta(days=1), time.min)
    query = """
        SELECT
            am.*,
            bre.lender_id,
            lender.lender_name,
            bre.criteria_missed,
            CASE
                WHEN bre.criteria_missed IS NULL
                  OR JSON_TYPE(bre.criteria_missed) = 'NULL'
                  OR JSON_LENGTH(bre.criteria_missed) = 0
                THEN 1
                ELSE 0
            END AS bre_eligible
        FROM application_master AS am
        INNER JOIN application_bre_logs AS bre
            ON bre.application_id = am.id
        LEFT JOIN mf_lenders AS lender
            ON lender.id = bre.lender_id
        WHERE am.created >= %s
          AND am.created < %s
    """
    params = [start_at, end_before]
    if lender_id is not None:
        query += " AND bre.lender_id = %s"
        params.append(lender_id)
    if eligible_only:
        query += """
          AND (
              bre.criteria_missed IS NULL
              OR JSON_TYPE(bre.criteria_missed) = 'NULL'
              OR JSON_LENGTH(bre.criteria_missed) = 0
          )
        """
    query += " ORDER BY am.id, bre.lender_id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    return query, tuple(params)


def format_query_for_print(query, params):
    """Render this query's known datetime/integer parameters for manual inspection."""
    rendered = []
    for value in params:
        if isinstance(value, datetime):
            rendered.append("'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'")
        elif isinstance(value, int):
            rendered.append(str(value))
        else:
            raise TypeError(f"Cannot safely render SQL parameter of type {type(value).__name__}")

    for value in rendered:
        query = query.replace("%s", value, 1)
    if "%s" in query:
        raise ValueError("Not all SQL parameters were rendered")
    return query.strip()


def fetch_applications(
    connection,
    start_date,
    end_date,
    lender_id=None,
    eligible_only=False,
    limit=None,
):
    """Fetch dated applications with their lender-specific BRE results."""
    query, params = build_application_query(
        start_date,
        end_date,
        lender_id=lender_id,
        eligible_only=eligible_only,
        limit=limit,
    )
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


def post_application(api_url, application):
    """Ask the Node API to run one eligible application through its lender stack."""
    response = requests.post(
        api_url,
        json={
            "application_id": application["id"],
            "lender_id": application["lender_id"],
        },
        headers={"x-api-key": API_KEY},
        timeout=API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ValueError("Node API returned an unsuccessful response")
    return result.get("data", {}).get("status", "processed")


def describe_http_error(error):
    """Report the HTTP status and API's safe error message without dumping response data."""
    response = error.response
    if response is None:
        return "HTTP error without a response"

    try:
        body = response.json()
    except ValueError:
        body = {}

    message = body.get("message") if isinstance(body, dict) else None
    try:
        reason = HTTPStatus(response.status_code).phrase
    except ValueError:
        reason = response.reason or "HTTP error"
    detail = f"HTTP {response.status_code} {reason}"
    if message:
        detail += f": {str(message)[:200]}"
    return detail


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch applications from application_master by creation date."
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START_DATE,
        help=f"first date, YYYY-MM-DD (default: {DEFAULT_START_DATE})",
    )
    parser.add_argument(
        "--end",
        default=DEFAULT_END_DATE,
        help=f"last date, YYYY-MM-DD inclusive (default: {DEFAULT_END_DATE})",
    )
    parser.add_argument(
        "--lender-id",
        type=int,
        default=DEFAULT_LENDER_ID,
        help=f"only include this lender's BRE result (default: {DEFAULT_LENDER_ID})",
    )
    parser.add_argument(
        "--include-ineligible",
        action="store_true",
        help="include BRE-rejected records too (eligible records are included by default)",
    )
    parser.add_argument("--limit", type=int, help="maximum number of applications to fetch")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="POST each record to the Node API (without this flag, only list records)",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Node API POST URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--print-sql",
        action="store_true",
        help="print the SQL for inspection without connecting to the database",
    )
    args = parser.parse_args(argv)

    try:
        query, params = build_application_query(
            args.start,
            args.end,
            lender_id=args.lender_id,
            eligible_only=not args.include_ineligible,
            limit=args.limit,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.print_sql:
        print(format_query_for_print(query, params) + ";")
        return 0

    if args.execute and not args.api_url.startswith(("http://", "https://")):
        parser.error("--api-url must start with http:// or https://")
    if args.execute and not API_KEY:
        parser.error("GENERIC_OTA_API_KEY must be configured before using --execute")

    connection = get_connection()
    failed = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            applications = list(cursor.fetchall())
        print(f"Fetched {len(applications)} application(s).")
        for application in applications:
            application_id = application["id"]
            if not args.execute:
                print(
                    f"application_id={application_id}, "
                    f"created={application['created']}, "
                    f"lender_id={application['lender_id']}, "
                    f"lender={application['lender_name']}, "
                    f"bre_eligible={bool(application['bre_eligible'])}, "
                    f"criteria_missed={application['criteria_missed']}"
                )
                continue

            try:
                outcome = post_application(args.api_url, application)
            except requests.HTTPError as exc:
                failed += 1
                print(
                    f"POST failed for application_id={application_id}: "
                    f"{describe_http_error(exc)}"
                )
            except (requests.RequestException, TypeError, ValueError, UnicodeDecodeError) as exc:
                failed += 1
                print(
                    f"POST failed for application_id={application_id}: "
                    f"{type(exc).__name__}"
                )
            else:
                print(
                    f"POST completed for application_id={application_id}: "
                    f"outcome={outcome}"
                )
        if args.execute:
            succeeded = len(applications) - failed
            print(f"POST results: {succeeded} succeeded, {failed} failed.")
        return 1 if failed else 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
