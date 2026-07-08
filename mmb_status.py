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

MYSQL_CONFIG = db_config()
MMB_LENDER_ID = 10
STALE_DAYS = 30

LEADS_QUERY = """
SELECT
    lm.id,
    lm.user_id,
    u.mobile
FROM lead_master AS lm
JOIN mf_users AS u ON u.id = lm.user_id
WHERE lm.lender_id = %s
  AND lm.status = 1
  AND lm.created >= NOW() - INTERVAL %s DAY
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


def fetch_leads():
    conn = pymysql.connect(**MYSQL_CONFIG)
    try:
        with conn.cursor() as cursor:
            cursor.execute(LEADS_QUERY, (MMB_LENDER_ID, STALE_DAYS))
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


def process_mmb_statuses():
    leads = fetch_leads()
    print(f"Found {len(leads)} lead(s) created in the last {STALE_DAYS} days")

    updated_count = 0
    skipped_count = 0
    failed_count = 0

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
            item = extract_status_payload(response_body)
            if not item:
                print(
                    f"  Skipped: API did not return data "
                    f"(message={response_body.get('message')})"
                )
                skipped_count += 1
                continue

            disburse_status = normalize_value(item.get("loan_status"))
            disburse_amount = normalize_value(
                item.get("disbursal_amount") or item.get("credit_limit")
            )
            disburse_datetime = normalize_value(item.get("disbursement_date"))

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

        time.sleep(1)

    print()
    print(
        f"Done. Updated={updated_count}, Skipped={skipped_count}, Failed={failed_count}"
    )


if __name__ == "__main__":
    try:
        process_mmb_statuses()
    except Exception as exc:
        print(f"MMB status sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
