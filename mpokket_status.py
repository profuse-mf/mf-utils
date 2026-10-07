import argparse
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
from mf_disbursals_store import persist_lender_status_poll

MYSQL_CONFIG = db_config()
MPOKKET_LENDER_ID = 9
STALE_DAYS = 30


def get_clickhouse_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DATABASE,
    )


def fetch_stale_leads(include_all=False):
    date_filter = (
        ""
        if include_all
        else f"AND toDate(updated) >= today() - {STALE_DAYS}"
    )
    query = f"""
        SELECT
            id,
            lender_ref_id
        FROM lead_master
        WHERE lender_id = 9
          {date_filter}
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


def persist_poll(
    lead_id,
    *,
    response_json,
    disburse_status=None,
    disburse_amount=None,
    disburse_datetime=None,
    apply_to_lead_and_disbursals=True,
):
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        return persist_lender_status_poll(
            conn,
            lead_id=lead_id,
            response_json=response_json,
            disburse_status=disburse_status,
            disburse_amount=disburse_amount,
            disburse_datetime=disburse_datetime,
            lender_id=MPOKKET_LENDER_ID,
            apply_to_lead_and_disbursals=apply_to_lead_and_disbursals,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_mpokket_statuses(include_all=False):
    leads = fetch_stale_leads(include_all=include_all)
    window = "all time" if include_all else f"last {STALE_DAYS} days"
    print(f"Found {len(leads)} lead(s) ({window})")

    updated_count = 0
    logged_only_count = 0
    failed_count = 0
    disbursals_count = 0

    for lead in leads:
        lead_id = lead["id"]
        request_id = lead["lender_ref_id"]
        print(f"Processing lead_id={lead_id}, request_id={request_id}")

        try:
            response_body = fetch_mpokket_status(request_id)
            print("  Response (raw):")
            print(
                f"    {json.dumps(response_body, ensure_ascii=False, default=str)}"
            )
            item = extract_status_payload(response_body) or {}
            disburse_status = get_acquisition_status(item)
            if not disburse_status and isinstance(response_body, dict):
                disburse_status = normalize_value(response_body.get("message"))
            disburse_amount = normalize_value(item.get("loan_disbursement_amount"))
            disburse_datetime = normalize_value(item.get("loan_disbursement_timestamp"))
            apply_full = isinstance(response_body, dict)

            result = persist_poll(
                lead_id,
                response_json=response_body,
                disburse_status=disburse_status,
                disburse_amount=disburse_amount,
                disburse_datetime=disburse_datetime,
                apply_to_lead_and_disbursals=apply_full,
            )
            if apply_full:
                updated_count += 1
                print(
                    f"  Persisted: disburse_status={disburse_status}, "
                    f"disburse_amount={disburse_amount}, "
                    f"disburse_datetime={disburse_datetime}, "
                    f"status_log_id={result.get('status_log_id')}"
                )
            else:
                logged_only_count += 1
            if result.get("disbursal"):
                disbursals_count += 1
                print(
                    f"  mf_disbursals {result['disbursal']}: "
                    f"application_id={result.get('application_id')}, "
                    f"lender_id={result.get('lender_id')}"
                )
        except Exception as exc:
            try:
                result = persist_poll(
                    lead_id,
                    response_json={"error": str(exc), "request_id": request_id},
                    apply_to_lead_and_disbursals=False,
                )
                logged_only_count += 1
                print(
                    f"  Logged error status_log_id={result.get('status_log_id')}",
                    file=sys.stderr,
                )
            except Exception as log_exc:
                failed_count += 1
                print(f"  Failed logging: {log_exc}", file=sys.stderr)
            print(f"  Failed: {exc}", file=sys.stderr)

    print()
    print(
        f"Done. Updated={updated_count}, LoggedOnly={logged_only_count}, "
        f"Failed={failed_count}, mf_disbursals={disbursals_count}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync mPokket lead statuses")
    parser.add_argument(
        "--all",
        dest="include_all",
        action="store_true",
        help=f"Process all matching leads (ignore {STALE_DAYS}-day window)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        args = parse_args()
        process_mpokket_statuses(include_all=args.include_all)
    except Exception as exc:
        print(f"Mpokket status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
