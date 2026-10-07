import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import pymysql

from config import (
    MMB_API_KEY,
    MMB_API_URL,
    MMB_MERCHANT_ID,
    db_config,
)
from mf_disbursals_store import persist_lender_status_poll
from mf_user_crypto import sql_aes_decrypt

MYSQL_CONFIG = db_config()
MMB_LENDER_ID = 10
STALE_DAYS = 30

LEADS_QUERY = f"""
SELECT
    lm.id,
    lm.user_id,
    lm.application_id,
    lm.lender_id,
    {sql_aes_decrypt("u.mobile", "mobile")}
FROM lead_master AS lm
JOIN mf_users AS u ON u.id = lm.user_id
WHERE lm.lender_id = %s
  AND lm.status = 1
  {{date_filter}}
ORDER BY lm.id
"""


def normalize_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "NA":
        return None
    return text


def normalize_phone(mobile):
    if mobile is None:
        return None
    phone = str(mobile).strip().replace("+", "")
    if phone.startswith("91") and len(phone) > 10:
        phone = phone[2:]
    return phone or None


def fetch_leads(include_all=False):
    date_filter = (
        "" if include_all else "AND lm.created >= NOW() - INTERVAL %s DAY"
    )
    query = LEADS_QUERY.format(date_filter=date_filter)
    params = (MMB_LENDER_ID,) if include_all else (MMB_LENDER_ID, STALE_DAYS)
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        conn.close()


def fetch_mmb_status(phone_number):
    payload = json.dumps(
        {
            "merchant_id": MMB_MERCHANT_ID,
            "phone_number": phone_number,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        MMB_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "api-key": MMB_API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"MMB API error {exc.code} for phone={phone_number}: {error_body}"
        ) from exc


def extract_status_payload(response_body):
    data = response_body.get("data")
    if isinstance(data, dict) and data:
        return data
    return None


def persist_poll(
    lead_id,
    *,
    response_json,
    disburse_status=None,
    disburse_amount=None,
    disburse_datetime=None,
    user_id=None,
    application_id=None,
    lender_id=None,
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
            user_id=user_id,
            application_id=application_id,
            lender_id=lender_id if lender_id is not None else MMB_LENDER_ID,
            apply_to_lead_and_disbursals=apply_to_lead_and_disbursals,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_mmb_statuses(include_all=False):
    leads = fetch_leads(include_all=include_all)
    window = "all time" if include_all else f"last {STALE_DAYS} days"
    print(f"Found {len(leads)} lead(s) ({window})")

    updated_count = 0
    logged_only_count = 0
    skipped_count = 0
    failed_count = 0
    disbursals_count = 0

    for lead in leads:
        lead_id = lead["id"]
        user_id = lead["user_id"]
        phone = normalize_phone(lead.get("mobile"))
        print(f"Processing lead_id={lead_id}, user_id={user_id}, mobile={phone}")

        if not phone:
            print("  Skipped: missing mobile")
            skipped_count += 1
            continue

        try:
            response_body = fetch_mmb_status(phone)
            print("  Response (raw):")
            print(
                f"    {json.dumps(response_body, ensure_ascii=False, default=str)}"
            )
            item = extract_status_payload(response_body) or {}
            disburse_status = normalize_value(item.get("loan_status"))
            if not disburse_status and isinstance(response_body, dict):
                disburse_status = normalize_value(response_body.get("message"))
            disburse_amount = normalize_value(
                item.get("disbursal_amount") or item.get("credit_limit")
            )
            disburse_datetime = normalize_value(item.get("disbursement_date"))
            apply_full = isinstance(response_body, dict)

            result = persist_poll(
                lead_id,
                response_json=response_body,
                disburse_status=disburse_status,
                disburse_amount=disburse_amount,
                disburse_datetime=disburse_datetime,
                user_id=user_id,
                application_id=lead.get("application_id"),
                lender_id=lead.get("lender_id") or MMB_LENDER_ID,
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
                    response_json={"error": str(exc), "phone": phone},
                    user_id=user_id,
                    application_id=lead.get("application_id"),
                    lender_id=lead.get("lender_id") or MMB_LENDER_ID,
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

        time.sleep(1)

    print()
    print(
        f"Done. Updated={updated_count}, LoggedOnly={logged_only_count}, "
        f"SkippedMissingKeys={skipped_count}, Failed={failed_count}, "
        f"mf_disbursals={disbursals_count}"
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Sync MMB lead statuses")
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
        process_mmb_statuses(include_all=args.include_all)
    except Exception as exc:
        print(f"MMB status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
