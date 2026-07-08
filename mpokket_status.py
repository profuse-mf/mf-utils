import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import clickhouse_connect
import pymysql

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    MPOKKET_API_BASE,
    MPOKKET_API_KEY,
    db_config,
)

MYSQL_CONFIG = db_config()
STALE_DAYS = 15


def get_clickhouse_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )


def fetch_stale_leads():
    query = f"""
        SELECT
            id,
            lender_ref_id
        FROM lead_master
        WHERE lender_id = 9
          AND toDate(updated) >= today() - {STALE_DAYS}
          AND lender_ref_id != ''
          AND ifNull(disburse_status, '') NOT IN (
              'Rejected On Request',
              'Rejected',
              'Disbursed'
          )
        ORDER BY id
    """
    client = get_clickhouse_client()
    result = client.query(query)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def extract_status_payload(response_body):
    if not response_body.get("success"):
        return None

    data = response_body.get("data")
    if isinstance(data, list):
        if not data:
            return None
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


def fetch_mpokket_status(request_id):
    params = urllib.parse.urlencode({"request_id": request_id})
    url = f"{MPOKKET_API_BASE}?{params}"

    request = urllib.request.Request(
        url,
        headers={"API-Key": MPOKKET_API_KEY},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Mpokket API error {exc.code} for request_id={request_id}: {error_body}"
        ) from exc


def get_acquisition_status(item):
    return normalize_value(
        item.get("acquisition_status") or item.get("aqusition_status")
    )


def update_lead_in_mysql(lead_id, disburse_status, disburse_amount, disburse_datetime):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE lead_master
                SET disburse_status = %s,
                    disburse_amount = %s,
                    disburse_datetime = %s,
                    disbursal_status_check = NOW()
                WHERE id = %s
                """,
                (disburse_status, disburse_amount, disburse_datetime, lead_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_mpokket_statuses():
    leads = fetch_stale_leads()
    print(f"Found {len(leads)} lead(s) updated in the last {STALE_DAYS} days")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for lead in leads:
        lead_id = lead["id"]
        request_id = lead["lender_ref_id"]
        print(f"Processing lead_id={lead_id}, request_id={request_id}")

        try:
            response_body = fetch_mpokket_status(request_id)
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: API did not return data "
                    f"(message={response_body.get('message')})"
                )
                skipped_count += 1
                continue

            disburse_status = get_acquisition_status(item)
            disburse_amount = normalize_value(item.get("loan_disbursement_amount"))
            disburse_datetime = normalize_value(item.get("loan_disbursement_timestamp"))

            update_lead_in_mysql(
                lead_id,
                disburse_status,
                disburse_amount,
                disburse_datetime,
            )
            updated_count += 1
            print(
                f"  Updated: disburse_status={disburse_status}, "
                f"disburse_amount={disburse_amount}, "
                f"disburse_datetime={disburse_datetime}"
            )
        except Exception as exc:
            failed_count += 1
            print(f"  Failed: {exc}", file=sys.stderr)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, Failed={failed_count}"
    )


if __name__ == "__main__":
    try:
        process_mpokket_statuses()
    except Exception as exc:
        print(f"Mpokket status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
